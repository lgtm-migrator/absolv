import os
import pickle
from typing import Callable, List, Optional, Tuple, Union

import numpy
from openff.toolkit.topology import Topology
from openff.toolkit.typing.engines.smirnoff import ForceField
from openff.utilities import temporary_cd
from openmm import openmm, unit
from tqdm import tqdm

from absolv.factories.alchemical import OpenMMAlchemicalFactory
from absolv.factories.coordinate import PACKMOLCoordinateFactory
from absolv.models import EquilibriumProtocol, State, TransferFreeEnergySchema
from absolv.simulations import AlchemicalOpenMMSimulation
from absolv.utilities.openmm import OpenMMPlatform
from absolv.utilities.topology import topology_to_atom_indices

SystemGenerator = Callable[[Topology, unit.Quantity], openmm.System]


class BaseRunner:
    """A utility class for setting up, running, and analyzing equilibrium
    free energy calculations.
    """

    @classmethod
    def _setup_solvent(
        cls,
        components: List[Tuple[str, int]],
        force_field: Union[ForceField, SystemGenerator],
        n_solute_molecules: int,
        n_solvent_molecules: int,
    ):
        """Creates the input files for a particular solvent phase.

        Args:
            components: A list of the form ``components[i] = (smiles_i, count_i)`` where
                ``smiles_i`` is the SMILES representation of component `i` and
                ``count_i`` is the number of corresponding instances of that component
                to create.

                It is expected that the first ``n_solute_molecules`` entries in this
                list correspond to molecules that will be alchemically transformed,
                and the remaining ``n_solvent_molecules`` entries correspond to molecules
                that will not.
            force_field: The force field, or a callable that transforms an OpenFF
                topology and a set of coordinates into an OpenMM system **without**
                any alchemical modifications, to run the calculations using.
            n_solute_molecules: The number of solute molecule.
            n_solvent_molecules: The number of solvent molecule.
        """

        is_vacuum = n_solvent_molecules == 0

        topology, coordinates = PACKMOLCoordinateFactory.generate(components)
        topology.box_vectors = None if is_vacuum else topology.box_vectors

        atom_indices = topology_to_atom_indices(topology)

        alchemical_indices = atom_indices[:n_solute_molecules]
        persistent_indices = atom_indices[n_solute_molecules:]

        if isinstance(force_field, ForceField):
            original_system = force_field.create_openmm_system(topology)
        else:
            original_system: openmm.System = force_field(topology, coordinates)

        alchemical_system = OpenMMAlchemicalFactory.generate(
            original_system, alchemical_indices, persistent_indices
        )

        topology.to_file("coords-initial.pdb", coordinates)
        numpy.save("coords-initial.npy", coordinates.value_in_unit(unit.angstrom))

        with open("system-chemical.xml", "w") as file:
            file.write(openmm.XmlSerializer.serializeSystem(original_system))

        with open("system-alchemical.xml", "w") as file:
            file.write(openmm.XmlSerializer.serializeSystem(alchemical_system))

        with open("topology.pkl", "wb") as file:
            pickle.dump(topology, file)

    @classmethod
    def setup(
        cls,
        schema: TransferFreeEnergySchema,
        force_field: Union[ForceField, SystemGenerator],
        directory: str = "absolv-experiment",
    ):
        """Prepare the input files needed to compute the free energy and store them
        in the specified directory.

        Args:
            schema: The schema defining the calculation to perform.
            force_field: The force field, or a callable that transforms an OpenFF
                topology and a set of coordinates into an OpenMM system **without**
                any alchemical modifications, to run the calculations using.
            force_field: The force field (or system generator) to use to generate
                the chemical system object.
            directory: The directory to create the input files in.
        """

        n_solute_molecules = schema.system.n_solute_molecules

        for solvent_index, components, n_solvent_molecules in zip(
            ("a", "b"),
            schema.system.to_components(),
            (schema.system.n_solvent_molecules_a, schema.system.n_solvent_molecules_b),
        ):

            solvent_directory = os.path.join(directory, f"solvent-{solvent_index}")
            os.makedirs(solvent_directory, exist_ok=False)

            with temporary_cd(solvent_directory):

                cls._setup_solvent(
                    components, force_field, n_solute_molecules, n_solvent_molecules
                )

    @classmethod
    def _run_solvent(
        cls,
        protocol: EquilibriumProtocol,
        state: State,
        platform: OpenMMPlatform,
        states: Optional[List[int]] = None,
    ):

        states = states if states is not None else list(range(protocol.n_states))

        with open("system-alchemical.xml", "r") as file:
            alchemical_system = openmm.XmlSerializer.deserializeSystem(file.read())

        with open("topology.pkl", "rb") as file:
            topology = pickle.load(file)

        coordinates = numpy.load("coords-initial.npy") * unit.angstrom

        for state_index in tqdm(states, desc="state", ncols=80):

            simulation = AlchemicalOpenMMSimulation(
                alchemical_system,
                coordinates,
                topology.box_vectors,
                State(
                    temperature=state.temperature,
                    pressure=None if topology.box_vectors is None else state.pressure,
                ),
                protocol,
                state_index,
                platform if topology.box_vectors is not None else "Reference",
            )

            simulation.run(os.path.join(f"state-{state_index}"))
