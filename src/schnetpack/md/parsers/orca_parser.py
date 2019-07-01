import logging
import os

import numpy as np

from ase import Atoms, units
from tqdm import tqdm

import schnetpack as spk
from schnetpack.atomistic import Properties

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))

# Conversion from ppm to atomic units. Alpha is the fine structure constant and 1e6 are the ppm
ppm2au = 1.0 / (units.alpha ** 2 * 1e6)


class OrcaParserException(Exception):
    pass


class OrcaParser:
    main_properties = [
        Properties.energy,
        Properties.forces,
        Properties.dipole_moment,
        Properties.polarizability,
        Properties.shielding,
    ]
    hessian_properties = [
        Properties.hessian,
        Properties.dipole_derivatives,
        Properties.polarizability_derivatives,
    ]

    file_extensions = {Properties.forces: ".engrad", Properties.hessian: ".oinp.hess"}

    atomistic = ["atoms", Properties.forces, Properties.shielding]

    def __init__(self, dbpath, properties, filter=None, mask_charges=False):

        self.dbpath = dbpath

        main_properties = []
        hessian_properties = []
        dummy_properties = []

        for p in properties:
            if p in self.main_properties:
                main_properties.append(p)
            elif p in self.hessian_properties:
                hessian_properties.append(p)
            else:
                print("Unrecognized property {:s}".format(p))

        all_properties = main_properties + hessian_properties + dummy_properties
        self.all_properties = all_properties
        self.atomsdata = spk.data.AtomsData(dbpath, required_properties=all_properties)

        # The main file parser is always needed
        self.main_parser = OrcaMainFileParser(properties=main_properties + ["atoms"])

        if len(hessian_properties) > 0:
            self.hessian_parser = OrcaHessianFileParser(properties=hessian_properties)
        else:
            self.hessian_parser = None

        # Set up filter dictionary to e.g. remove numerically unstable solvent computations
        self.filter = filter

        # If requested, mask Q charges introduced by Orca
        self.mask_charges = mask_charges

    def parse_data(self, data_files, buffer_size=10):
        atom_buffer = []
        property_buffer = []

        for file in tqdm(sorted(data_files), ncols=100):

            if os.path.exists(file):

                atoms, properties = self._parse_molecule(file)

                if properties is not None:
                    # Filter properties for problematic values
                    if self.filter is not None:
                        filtered = False
                        for p in self.filter:
                            if np.linalg.norm(properties[p]) > self.filter[p]:
                                filtered = True
                                logging.info(f"Filtered output {file} due to {p}")
                        if not filtered:
                            atom_buffer.append(atoms)
                            property_buffer.append(properties)
                    else:
                        atom_buffer.append(atoms)
                        property_buffer.append(properties)

                    if len(atom_buffer) >= buffer_size:
                        self.atomsdata.add_systems(atom_buffer, property_buffer)
                        atom_buffer = []
                        property_buffer = []

        # Collect leftovers
        if len(atom_buffer) > 0:
            self.atomsdata.add_systems(atom_buffer, property_buffer)

        metadata = {}
        self.atomsdata.set_metadata(metadata)

    def _parse_molecule(self, datafile):

        # check if computation converged
        if not self._check_convergence(datafile):
            return None, None

        # Get main properties
        self.main_parser.parse_file(datafile)
        main_properties = self.main_parser.get_parsed()

        if self.mask_charges:
            main_properties = self._mask_charges(main_properties)

        atoms = None
        properties = {}
        for p in main_properties:
            if main_properties[p] is None:
                print("Error parsing {:s}".format(p))
                return None, None
            elif p == "atoms":
                atypes, coords = main_properties[p]
                atoms = Atoms(atypes, coords)
            else:
                properties[p] = main_properties[p].astype(np.float32)

        if self.hessian_parser is not None:
            hessian_file = (
                os.path.splitext(datafile)[0] + self.file_extensions["hessian"]
            )
            if not os.path.exists(hessian_file):
                print("Could not open Hessian file {:s}".format(hessian_file))
                return atoms, None
            else:
                self.hessian_parser.parse_file(hessian_file)
                hessian_properties = self.hessian_parser.get_parsed()

                for p in hessian_properties:
                    if p is None:
                        return atoms, None
                    elif p == Properties.dipole_derivatives:
                        properties[p] = format_dipole_derivatives(hessian_properties[p])
                    elif p == Properties.polarizability_derivatives:
                        properties[p] = format_polarizability_derivatives(
                            hessian_properties[p]
                        )
                    else:
                        properties[p] = hessian_properties[p]

        return atoms, properties

    def _check_convergence(self, datafile):
        """
        Check whether calculation is converged.
        """
        flag = open(datafile).readlines()[-2].strip()

        if not flag == "****ORCA TERMINATED NORMALLY****":
            return False
        else:
            return True

    def _mask_charges(self, main_properties):
        """
        Remove the external charges Q introduced in orca input file. This is
        only necessary, if the charges are given in the input file. This in
        turn is necessary to get the right shielding tensors, as Orca is buggy
        in this case. All other properties need to be taken from a computation
        with external charges given in a file, since external charges in the
        input file are added to the potential energy and the dipole moment...
        """
        n_atoms = np.sum(main_properties["atoms"][0] != "Q")

        for p in main_properties:
            if p in self.atomistic:
                if p == "atoms":
                    main_properties[p][0] = main_properties[p][0][:n_atoms]
                    main_properties[p][1] = main_properties[p][1][:n_atoms]
                else:
                    main_properties[p] = main_properties[p][:n_atoms]

        return main_properties


def format_dipole_derivatives(property):
    """
    Format is Natoms x (dx dy dz) x (property x y z)
    """
    N, _ = property.shape
    N = N // 3
    property = property.reshape(N, 3, 3)
    return property


def format_polarizability_derivatives(property):
    """
    Format is Natoms x (dx dy dz) x (property Tensor)
    """
    N, _ = property.shape
    N = N // 3
    property = property.reshape(N, 3, 6)
    triu_idx = np.triu_indices(3)
    reshaped = np.zeros((N, 3, 3, 3))
    reshaped[:, :, triu_idx[0], triu_idx[1]] = property
    reshaped[:, :, triu_idx[1], triu_idx[0]] = property
    return reshaped


class OrcaOutputParser:
    """
    Basic Orca output parser class. Parses an Orca output file according to the parsers specified in the 'parsers'
    dictionary. Parsed data is stored in an dictionary, using the same keys as the parsers. If a list of formatters is
    provided to a parser, a list of the parsed entries is stored in the ouput dictionary.

    Args:
        parsers (dict[str->callable]): dictionary of OrcaPropertyParser, each with their own OrcaFormatter.
    """

    def __init__(self, parsers):
        self.parsers = parsers
        self.parsed = None

    def parse_file(self, path):
        """
        Open the file and iterate over its lines, applying all parsers. In the end, all data is collected in a
        dictionary.

        Args:
            path (str): path to Orca output file.
        """
        # Reset for new file
        for parser in self.parsers:
            self.parsers[parser].reset()

        with open(path, "r") as f:
            for line in f:
                for parser in self.parsers:
                    self.parsers[parser].parse_line(line)

        self.parsed = {}

        for parser in self.parsers:
            self.parsed[parser] = self.parsers[parser].get_parsed()

    def get_parsed(self):
        """
        Get parsed data.

        Returns:
            dict[str->list]: Dictionary of data entries according to parser keys.
        """
        return self.parsed


class OrcaFormatter:
    """
    Format raw Orca data collected by an OrcaPropertyParser. Behavior is determined by the datatype option.

    Args:
        position (int): Position to start formatting. If no stop is provided returns only value at position, otherwise
                        all values between position and stop are returned. (Only used for 'vector' mode)
        stop (int, optional): Stop value for range. (Only used for 'vector' mode)
        datatype (str, optional): Change formatting behavior. The possible options are:
                                  'vector': Formats data between position and stop argument, if provided converting it
                                            to the type given in the converter.
                                  'matrix': Formats collected matrix data into the shape of a square, symmetric
                                            numpy.ndarray. Ignores other options.
        converter (type, optional): Convert data to type. (Only used for 'vector' mode)
        default (float): Default value to be returned if nothing is parsed (e.g. 1.0 for vacuum in case of
                         dielectric constant.
        skip_first (int): If not None, skip the first N lines (default=None).
    """

    def __init__(
        self,
        position,
        stop=None,
        datatype="vector",
        converter=float,
        skip_first=None,
        unit=None,
        default=None,
    ):
        self.position = position
        self.stop = stop
        self.datatype = datatype
        self.converter = converter
        self.matrix_dim = None
        self.skip_first = skip_first
        self.unit = unit
        self.default = default

    def format(self, parsed):
        if parsed is None:
            if self.default is not None:
                return np.array([self.default])
            else:
                return None
        else:
            if self.skip_first is not None:
                parsed = parsed[self.skip_first :]

        if len(parsed) == 0:
            return None

        elif self.datatype == "vector":
            formatted = self._format_vector(parsed)
        elif self.datatype == "matrix":
            formatted = self._format_matrix(parsed)
        elif self.datatype == "shielding":
            formatted = self._format_shielding(parsed)
        else:
            raise NotImplementedError(
                "Unrecognized data type {:s}".format(self.datatype)
            )

        if self.unit is not None:
            formatted *= self.unit

        return formatted

    def _format_vector(self, parsed):
        vector = []
        for line in parsed:
            line = line.split()
            if self.stop is None:
                vector.append(self.converter(line[self.position]))
            else:
                vector.append(
                    [self.converter(x) for x in line[self.position : self.stop]]
                )

        vector = np.array(vector)

        # Remove trailing dimension if only one line is read (for dipole moment)
        if vector.shape[0] == 1 and vector.size != 1:
            vector = vector[0]

        return vector

    def _format_matrix(self, parsed):

        n_entries = len(parsed[1].split())

        # Get matrix dimensions
        for line in parsed[1:]:
            line = line.split()
            if len(line) != n_entries:
                self.matrix_dim = int(line[0]) + 1

        subdata = [
            parsed[i : i + self.matrix_dim + 1]
            for i in range(0, len(parsed), self.matrix_dim + 1)
        ]

        matrix = [[] for _ in range(self.matrix_dim)]

        for block in subdata:
            for i, entry in enumerate(block[1:]):
                matrix[i] += [float(x) for x in entry.split()[1:]]

        matrix = np.array(matrix)
        return matrix

    def _format_shielding(self, parsed):
        shielding = []
        current_shielding = []
        parse = False
        for line in parsed:
            if line.startswith("Total shielding tensor (ppm):"):
                parse = True
            elif parse:
                if line.startswith("Diagonalized sT*s matrix:"):
                    shielding.append(current_shielding)
                    current_shielding = []
                    parse = False
                else:
                    current_shielding.append([float(x) for x in line.split()])
            else:
                continue

        shielding = np.array(shielding)
        return shielding


class OrcaPropertyParser:
    """
    Basic property parser for ORCA output files. Takes a start flag and a stop flag/list of stop flags and collects
    the data entries in between. If a formatter is provided, the data is formatted accordingly upon retrieval. Operates
    in a line-wise fashion.

    Args:
        start (str): begins to collect data starting from this string
        stop (str/list(str)): stops data collection if any of these strings is encounteres
        formatters (object): OrcaFormatter to convert collected data
    """

    def __init__(self, start, stop, formatters=None):
        self.start = start
        self.stop = stop
        self.formatters = formatters

        self.read = False
        self.parsed = None

    def parse_line(self, line):
        """
        Parses a line in the output file and updates Parser.

        Args:
            line (str): line of Orca output file
        """
        line = line.strip()
        if line.startswith("---------") or len(line) == 0:
            pass
        # if line.startswith("*********") or len(line) == 0:
        #     pass
        elif line.startswith(self.start):
            # Avoid double reading and restart for multiple files and repeated instances of data.
            self.parsed = []
            self.read = True
            # For single line output
            if self.stop is None:
                self.parsed.append(line)
                self.read = False
        elif self.read:
            # Check for stops
            if isinstance(self.stop, list):
                for stop in self.stop:
                    if self.read and line.startswith(stop):
                        self.read = False
                if self.read:
                    self.parsed.append(line)
            else:
                if line.startswith(self.stop):
                    self.read = False
                else:
                    self.parsed.append(line)

    def get_parsed(self):
        """
        Returns data, if formatters are specified in the corresponding format.
        """
        if self.formatters is None:
            return self.parsed
        elif hasattr(self.formatters, "__iter__"):
            return [formatter.format(self.parsed) for formatter in self.formatters]
        else:
            return self.formatters.format(self.parsed)

    def reset(self):
        """
        Reset state of parser
        """
        self.read = False
        self.parsed = None


class OrcaMainFileParser(OrcaOutputParser):
    properties = [
        "atoms",
        Properties.forces,
        Properties.energy,
        Properties.dipole_moment,
        Properties.polarizability,
        Properties.shielding,
    ]

    starts = {
        "atoms": "CARTESIAN COORDINATES (ANGSTROEM)",
        Properties.forces: "CARTESIAN GRADIENT",
        Properties.energy: "FINAL SINGLE POINT ENERGY",
        Properties.dipole_moment: "Total Dipole Moment",
        Properties.polarizability: "The raw cartesian tensor (atomic units):",
        Properties.shielding: "CHEMICAL SHIFTS",
    }

    stops = {
        "atoms": "CARTESIAN COORDINATES (A.U.)",
        Properties.forces: "Difference to translation invariance",
        Properties.energy: None,
        Properties.dipole_moment: None,
        Properties.polarizability: "diagonalized tensor:",
        Properties.shielding: "CHEMICAL SHIELDING SUMMARY",
    }

    formatters = {
        "atoms": (
            OrcaFormatter(0, converter=str),
            OrcaFormatter(1, stop=4, unit=1.0 / units.Bohr),
        ),
        Properties.energy: OrcaFormatter(4),
        Properties.forces: OrcaFormatter(3, stop=6, unit=-1.0),
        Properties.dipole_moment: OrcaFormatter(4, stop=7),
        Properties.polarizability: OrcaFormatter(0, stop=4),
        Properties.shielding: OrcaFormatter(None, datatype="shielding", unit=ppm2au),
    }

    def __init__(self, properties=None):

        if properties is None:
            to_parse = self.properties
        else:
            to_parse = []
            for p in properties:
                if p not in self.properties:
                    print("Cannot parse property {:s}".format(p))
                else:
                    to_parse.append(p)

        parsers = {
            p: OrcaPropertyParser(
                self.starts[p], self.stops[p], formatters=self.formatters[p]
            )
            for p in to_parse
        }

        super(OrcaMainFileParser, self).__init__(parsers)


class OrcaHessianFileParser(OrcaMainFileParser):
    properties = [
        Properties.hessian,
        Properties.dipole_derivatives,
        Properties.polarizability_derivatives,
    ]

    starts = {
        Properties.hessian: "$hessian",
        Properties.dipole_derivatives: "$dipole_derivatives",
        Properties.polarizability_derivatives: "$polarizability_derivatives",
    }

    stops = {
        Properties.hessian: "$vibrational_frequencies",
        Properties.dipole_derivatives: "#",
        Properties.polarizability_derivatives: "#",
    }

    formatters = {
        Properties.hessian: OrcaFormatter(None, datatype="matrix", skip_first=1),
        Properties.dipole_derivatives: OrcaFormatter(0, stop=4, skip_first=1),
        Properties.polarizability_derivatives: OrcaFormatter(0, stop=6, skip_first=1),
    }

    def __init__(self, properties=None):
        super(OrcaHessianFileParser, self).__init__(properties)
