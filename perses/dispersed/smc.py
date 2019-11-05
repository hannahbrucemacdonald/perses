import simtk.openmm as openmm
import openmmtools.cache as cache
from typing import List, Tuple, Union, NamedTuple
import os
import copy
import openmmtools.cache as cache

import openmmtools.mcmc as mcmc
import openmmtools.integrators as integrators
import openmmtools.states as states
from openmmtools.states import ThermodynamicState, CompoundThermodynamicState, SamplerState
import numpy as np
import mdtraj as md
from perses.annihilation.relative import HybridTopologyFactory
import mdtraj.utils as mdtrajutils
import pickle
import simtk.unit as unit
import tqdm
from perses.tests.utils import compute_potential_components
from openmmtools.constants import kB
import pdb
import logging
import tqdm
from sys import getsizeof
import time
from collections import namedtuple
from perses.annihilation.lambda_protocol import LambdaProtocol
from perses.annihilation.lambda_protocol import RelativeAlchemicalState, LambdaProtocol
import random
import pymbar
import dask.distributed as distributed

# Instantiate logger
logging.basicConfig(level = logging.NOTSET)
_logger = logging.getLogger("sMC")
_logger.setLevel(logging.DEBUG)

#cache.global_context_cache.platform = openmm.Platform.getPlatformByName('Reference') #this is just a local version
EquilibriumFEPTask = namedtuple('EquilibriumInput', ['sampler_state', 'inputs', 'outputs'])
NonequilibriumFEPTask = namedtuple('NonequilibriumFEPTask', ['particle', 'inputs'])

class DaskClient(object):
    """
    This class manages the dask scheduler.
    Parameters
    ----------
    LSF: bool, default False
        whether we are using the LSF dask Client
    num_processes: int, default 2
        number of processes to run.  If not LSF, this argument does nothing
    adapt: bool, default False
        whether to use an adaptive scheduler.  If not LSF, this argument does nothing
    """
    def __init__(self):
        _logger.info(f"Initializing DaskClient")

    def activate_client(self,
                        LSF = True,
                        num_processes = 2,
                        adapt = False):

        if LSF:
            from dask_jobqueue import LSFCluster
            cluster = LSFCluster()
            self._adapt = adapt
            self.num_processes = num_processes

            if self._adapt:
                _logger.debug(f"adapting cluster from 1 to {self.num_processes} processes")
                cluster.adapt(minimum = 2, maximum = self.num_processes, interval = "1s")
            else:
                _logger.debug(f"scaling cluster to {self.num_processes} processes")
                cluster.scale(self.num_processes)

            _logger.debug(f"scheduling cluster with client")
            self.client = distributed.Client(cluster)
        else:
            self.client = None
            self._adapt = False
            self.num_processes = 0

    def deactivate_client(self):
        """
        NonequilibriumSwitchingFEP is not pickleable with the self.client or self.cluster activated.
        This must be called before pickling
        """
        if self.client is not None:
            self.client.close()
            self.client = None

    def scatter(self, df):
        """
        wrapper to scatter the local data df
        """
        if self.client is None:
            #don't actually scatter
            return df
        else:
            return self.client.scatter(df)

    def deploy(self, func, arguments):
        """
        wrapper to map a function and its arguments to the client for scheduling
        Arguments
        ---------
        func : function to map
            arguments: tuple of the arguments that the function will take
        argument : tuple of argument lists
        Returns
        ---------
        futures
        """
        if self.client is None:
            if len(arguments) == 1:
                futures = [func(plug) for plug in arguments[0]]
            else:
                futures = [func(*plug) for plug in zip(*arguments)]
        else:
            futures = self.client.map(func, *arguments)
        return futures

    def gather_results(self, futures):
        """
        wrapper to gather a function given its arguments
        Arguments
        ---------
        futures : future pointers

        Returns
        ---------
        results
        """
        if self.client is None:
            return futures
        else:
            results = self.client.gather(futures)
            return results

    def gather_actor_result(self, future):
        """
        wrapper to pull the .result() of a method called to an actor
        """
        if self.client is None:
            return future
        else:
            result = future.result()
            return result

    def launch_actor(self, _class):
        """
        wrapper to launch an actor

        Arguments
        ---------
        _class : class object
            class to put on a worker

        Returns
        ---------
        actor : dask.distributed.Actor pointer (future)
        """
        if self.client is not None:
            future = self.client.submit(_class, actor=True)  # Create a _class on a worker
            actor = future.result()                    # Get back a pointer to that object
            return actor
        else:
            actor = _class()
            return actor

    def wait(self, futures):
        """
        wrapper to wait until futures are complete.
        """
        if self.client is None:
            pass
        else:
            distributed.wait(futures)

class SequentialMonteCarlo(DaskClient):
    """
    This class represents an sMC particle that runs a nonequilibrium switching protocol.
    It is a batteries-included engine for conducting sequential Monte Carlo sampling.

    WARNING: take care in writing trajectory file as saving positions to memory is costly.  Either do not write the configuration or save sparse positions.
    """

    def __init__(self,
                 factory,
                 lambda_protocol = 'default',
                 temperature = 300 * unit.kelvin,
                 trajectory_directory = 'test',
                 trajectory_prefix = 'out',
                 atom_selection = 'not water',
                 timestep = 1 * unit.femtoseconds,
                 collision_rate = 1 / unit.picoseconds,
                 eq_splitting_string = 'V R O R V',
                 neq_splitting_string = 'V R O R V',
                 ncmc_save_interval = None,
                 measure_shadow_work = False,
                 LSF = False,
                 num_processes = 2,
                 adapt = False):
        """
        Parameters
        ----------
        factory : perses.annihilation.relative.HybridTopologyFactory - compatible object
        lambda_protocol : str, default 'default'
            the flavor of scalar lambda protocol used to control electrostatic, steric, and valence lambdas
        temperature : float unit.Quantity
            Temperature at which to perform the simulation, default 300K
        trajectory_directory : str, default 'test'
            Where to write out trajectories resulting from the calculation. If None, no writing is done.
        trajectory_prefix : str, default None
            What prefix to use for this calculation's trajectory files. If none, no writing is done.
        atom_selection : str, default not water
            MDTraj selection syntax for which atomic coordinates to save in the trajectories. Default strips
            all water.
        timestep : float unit.Quantity, default 1 * units.femtoseconds
            the timestep for running MD
        collision_rate : float unit.Quantity, default 1 / unit.picoseconds
            the collision rate for running MD
        eq_splitting_string : str, default 'V R O R V'
            The integrator splitting to use for equilibrium simulation
        neq_splitting_string : str, default 'V R O R V'
            The integrator splitting to use for nonequilibrium switching simulation
        ncmc_save_interval : int, default None
            interval with which to write ncmc trajectory.  If None, trajectory will not be saved.
            We will assert that the n_lambdas % ncmc_save_interval = 0; otherwise, the protocol will not be complete
        measure_shadow_work : bool, default False
            whether to measure the shadow work of the integrator.
            WARNING : this is not currently supported
        LSF: bool, default False
            whether we are using the LSF dask Client
        num_processes: int, default 2
            number of processes to run.  If not LSF, this argument does nothing
        adapt: bool, default False
            whether to use an adaptive scheduler.  If not LSF, this argument does nothing
        """

        _logger.info(f"Initializing SequentialMonteCarlo")

        #pull necessary attributes from factory
        self.factory = factory

        #context cache
        self.context_cache = cache.global_context_cache

        #use default protocol
        self.lambda_protocol = lambda_protocol

        #handle both eq and neq parameters
        self.temperature = temperature
        self.timestep = timestep
        self.collision_rate = collision_rate

        self.measure_shadow_work = measure_shadow_work
        if measure_shadow_work:
            raise Exception(f"measure_shadow_work is not currently supported.  Aborting!")


        #handle equilibrium parameters
        self.eq_splitting_string = eq_splitting_string

        #handle storage and names
        self.trajectory_directory = trajectory_directory
        self.trajectory_prefix = trajectory_prefix
        self.atom_selection = atom_selection

        #handle neq parameters
        self.neq_splitting_string = neq_splitting_string
        self.ncmc_save_interval = ncmc_save_interval

        #lambda states:
        self.lambda_endstates = {'forward': [0.0,1.0], 'reverse': [1.0, 0.0]}

        #instantiate trajectory filenames
        if self.trajectory_directory and self.trajectory_prefix:
            self.write_traj = True
            self.eq_trajectory_filename = {lambda_state: os.path.join(os.getcwd(), self.trajectory_directory, f"{self.trajectory_prefix}.eq.lambda_{lambda_state}.h5") for lambda_state in self.lambda_endstates['forward']}
            self.neq_traj_filename = {direct: os.path.join(os.getcwd(), self.trajectory_directory, f"{self.trajectory_prefix}.neq.lambda_{direct}") for direct in self.lambda_endstates.keys()}
            self.topology = self.factory.hybrid_topology
        else:
            self.write_traj = False
            self.eq_trajectory_filename = {0: None, 1: None}
            self.neq_traj_filename = {'forward': None, 'reverse': None}
            self.topology = None

        # subset the topology appropriately:
        self.atom_selection_string = atom_selection
        # subset the topology appropriately:
        if self.atom_selection_string is not None:
            atom_selection_indices = self.factory.hybrid_topology.select(self.atom_selection_string)
            self.atom_selection_indices = atom_selection_indices
        else:
            self.atom_selection_indices = None

        # instantiating equilibrium file/rp collection dicts
        self._eq_dict = {0: [], 1: [], '0_decorrelated': None, '1_decorrelated': None, '0_reduced_potentials': [], '1_reduced_potentials': []}
        self._eq_files_dict = {0: [], 1: []}
        self._eq_timers = {0: [], 1: []}
        self._neq_timers = {'forward': [], 'reverse': []}

        #instantiate nonequilibrium work dicts: the keys indicate from which equilibrium thermodynamic state the neq_switching is conducted FROM (as opposed to TO)
        self.cumulative_work = {'forward': [], 'reverse': []}
        self.incremental_work = copy.deepcopy(self.cumulative_work)
        self.shadow_work = copy.deepcopy(self.cumulative_work)
        self.nonequilibrium_timers = copy.deepcopy(self.cumulative_work)
        self.total_jobs = 0
        #self.failures = copy.deepcopy(self.cumulative_work)
        self.dg_EXP = copy.deepcopy(self.cumulative_work)
        self.dg_BAR = None


        # create an empty dict of starting and ending sampler_states
        self.start_sampler_states = {_direction: [] for _direction in ['forward', 'reverse']}
        self.end_sampler_states = {_direction: [] for _direction in ['forward', 'reverse']}

        #instantiate thermodynamic state
        lambda_alchemical_state = RelativeAlchemicalState.from_system(self.factory.hybrid_system)
        lambda_alchemical_state.set_alchemical_parameters(0.0, LambdaProtocol(functions = self.lambda_protocol))
        self.thermodynamic_state = CompoundThermodynamicState(ThermodynamicState(self.factory.hybrid_system, temperature = self.temperature),composable_states = [lambda_alchemical_state])

        # set the SamplerState for the lambda 0 and 1 equilibrium simulations
        sampler_state = SamplerState(self.factory.hybrid_positions,
                                          box_vectors=self.factory.hybrid_system.getDefaultPeriodicBoxVectors())
        self.sampler_states = {0: copy.deepcopy(sampler_state), 1: copy.deepcopy(sampler_state)}

        #Dask implementables
        self.LSF = LSF
        self.num_processes = num_processes
        self.adapt = adapt

    def launch_LocallyOptimalAnnealing(self, lambdas):
        """
        Call LocallyOptimalAnnealing with the number of particles and a protocol.

        Arguments
        ----------
        lambdas : np.array
            the lambdas denoting the target distributions

        Returns
        -------
        LOA_actor : dask.distributed.actor (or class object) of smc.LocallyOptimalAnnealing
            actor pointer if self.LSF, otherwise class object
        """
        if self.LSF:
            LOA_actor = self.launch_actor(LocallyOptimalAnnealing)
        else:
            LOA_actor = self.launch_actor(LocallyOptimalAnnealing)

        actor_bool = LOA_actor.initialize(thermodynamic_state = self.thermodynamic_state,
                                          lambda_protocol = self.lambda_protocol,
                                          timestep = self.timestep,
                                          collision_rate = self.collision_rate,
                                          temperature = self.temperature,
                                          neq_splitting_string = self.neq_splitting_string,
                                          ncmc_save_interval = self.ncmc_save_interval,
                                          topology = self.topology,
                                          subset_atoms = self.topology.select(self.atom_selection_string),
                                          measure_shadow_work = self.measure_shadow_work)
        if self.LSF:
            assert self.gather_actor_result(actor_bool), f"Dask initialization failed"
        else:
            assert actor_bool, f"local initialization failed"

        return LOA_actor

    def AIS(self,
            num_particles,
            protocol_length,
            directions = ['forward','reverse'],
            num_integration_steps = 1,
            return_timer = False):
        """
        Conduct vanilla AIS. with a linearly interpolated lambda protocol

        Arguments
        ----------
        num_particles : int
            number of particles to run in each direction
        protocol_length : int
            number of lambdas
        directions : list of str, default ['forward', 'reverse']
            the directions to run.
        num_integration_steps : int
            number of integration steps per proposal
        return_timer : bool, default False
            whether to time the annealing protocol
        """
        self.activate_client(LSF = self.LSF,
                            num_processes = self.num_processes,
                            adapt = self.adapt)

        for _direction in directions:
            assert _direction in ['forward', 'reverse'], f"direction {_direction} is not an appropriate direction"
        protocols = {}
        for _direction in directions:
            if _direction == 'forward':
                protocol = np.linspace(0, 1, protocol_length)
            elif _direction == 'reverse':
                protocol = np.linspace(1, 0, protocol_length)
            protocols.update({_direction: protocol})

        if self.LSF: #we have to figure out how many actors to make
            if not self.adapt:
                num_actors = self.num_processes
                particles_per_actor = [num_particles // num_actors] * num_actors
                particles_per_actor[-1] += num_particles % num_actors
            else:
                raise Exception(f"the client is adaptable, but AIS does not currently support an adaptive client")
        else:
            #we only create one local actor and put all of the particles on it
            num_actors = 1
            particles_per_actor = [num_particles]

        #now we have to launch the LocallyOptimalAnnealing actors
        AIS_actors = {_direction: {} for _direction in directions}
        for _direction in directions:
            for num_anneals in particles_per_actor: #particles_per_actor is a list of ints where each element is the number of annealing jobs in the actor
                _actor = self.launch_LocallyOptimalAnnealing(protocols[_direction])
                AIS_actors[_direction].update({_actor : []})
                for _ in range(num_anneals): #launch num_anneals annealing jobs
                    sampler_state = self.pull_trajectory_snapshot(0) if _direction == 'forward' else self.pull_trajectory_snapshot(1)
                    if self.ncmc_save_interval is not None: #check if we should make 'trajectory_filename' not None
                        noneq_trajectory_filename = self.neq_traj_filename[_direction] + f".iteration_{self.total_jobs:04}.h5"
                        self.total_jobs += 1
                    else:
                        noneq_trajectory_filename = None

                    actor_future = _actor.anneal(sampler_state = sampler_state,
                                                 lambdas = protocols[_direction],
                                                 noneq_trajectory_filename = noneq_trajectory_filename,
                                                 num_integration_steps = num_integration_steps,
                                                 return_timer = return_timer,
                                                 return_sampler_state = False)

                    AIS_actors[_direction][_actor].append(actor_future)

        #now that the actors are gathered, we can collect the results and put them into class attributes
        for _direction in AIS_actors.keys():
            _result_lst = [[self.gather_actor_result(_future) for _future in AIS_actors[_direction][_actor]] for _actor in AIS_actors[_direction].keys()]
            flattened_result_lst = [item for sublist in _result_lst for item in sublist]
            [self.incremental_work[_direction].append(item[0]) for item in flattened_result_lst]
            [self.nonequilibrium_timers[_direction].append(item[2]) for item in flattened_result_lst]

        #compute the free energy
        self.compute_free_energy()

        #deactivate_client
        self.deactivate_client()

    def compute_free_energy(self):
        """
        given self.cumulative_work, compute the free energy profile
        """
        for _direction, works in self.incremental_work.items():
            if works != []:
                self.cumulative_work[_direction] = np.vstack([np.cumsum(work) for work in works])
                final_works = self.cumulative_work[_direction][:,-1]
                self.dg_EXP[_direction] = pymbar.EXP(final_works)

        if all(work != [] for work in self.cumulative_work.values()): #then we can do BAR estimator
            self.dg_BAR = pymbar.BAR(self.cumulative_work['forward'][:,-1], self.cumulative_work['reverse'][:,-1])




    def minimize_sampler_states(self):
        # initialize by minimizing
        for state in self.lambda_endstates['forward']: # 0.0, 1.0
            self.thermodynamic_state.set_alchemical_parameters(state, LambdaProtocol(functions = self.lambda_protocol))
            SequentialMonteCarlo.minimize(self.thermodynamic_state, self.sampler_states[int(state)])

    def pull_trajectory_snapshot(self, endstate):
        """
        Draw randomly a single snapshot from self._eq_files_dict

        Parameters
        ----------
        endstate: int
            lambda endstate from which to extract an equilibrated snapshot, either 0 or 1
        Returns
        -------
        sampler_state: openmmtools.SamplerState
            sampler state with positions and box vectors if applicable
        """
        #pull a random index
        index = random.choice(self._eq_dict[f"{endstate}_decorrelated"])
        files = [key for key in self._eq_files_dict[endstate].keys() if index in self._eq_files_dict[endstate][key]]
        assert len(files) == 1, f"files: {files} doesn't have one entry; index: {index}, eq_files_dict: {self._eq_files_dict[endstate]}"
        file = files[0]
        file_index = self._eq_files_dict[endstate][file].index(index)

        #now we load file as a traj and create a sampler state with it
        traj = md.load_frame(file, file_index)
        positions = traj.openmm_positions(0)
        box_vectors = traj.openmm_boxes(0)
        sampler_state = SamplerState(positions, box_vectors = box_vectors)

        return sampler_state

    def equilibrate(self,
                    n_equilibration_iterations = 1,
                    n_steps_per_equilibration = 5000,
                    endstates = [0,1],
                    max_size = 1024*1e3,
                    decorrelate=False,
                    timer = False,
                    minimize = False):
        """
        Run the equilibrium simulations a specified number of times at the lambda 0, 1 states. This can be used to equilibrate
        the simulation before beginning the free energy calculation.

        Parameters
        ----------
        n_equilibration_iterations : int; default 1
            number of equilibrium simulations to run, each for lambda = 0, 1.
        n_steps_per_equilibration : int, default 5000
            number of integration steps to take in an equilibration iteration
        endstates : list, default [0,1]
            at which endstate(s) to conduct n_equilibration_iterations (either [0] ,[1], or [0,1])
        max_size : float, default 1.024e6 (bytes)
            number of bytes allotted to the current writing-to file before it is finished and a new equilibrium file is initiated.
        decorrelate : bool, default False
            whether to parse all written files serially and remove correlated snapshots; this returns an ensemble of iid samples in theory.
        timer : bool, default False
            whether to trigger the timing in the equilibration; this adds an item to the EquilibriumResult, which is a list of times for various
            processes in the feptask equilibration scheme.
        minimize : bool, default False
            Whether to minimize the sampler state before conducting equilibration. This is passed directly to feptasks.run_equilibration
        Returns
        -------
        equilibrium_result : perses.dispersed.feptasks.EquilibriumResult
            equilibrium result namedtuple
        """

        _logger.debug(f"conducting equilibration")
        for endstate in endstates:
            assert endstate in [0, 1], f"the endstates contains {endstate}, which is not in [0, 1]"

        # run a round of equilibrium
        _logger.debug(f"iterating through endstates to submit equilibrium jobs")
        EquilibriumFEPTask_list = []
        for state in endstates: #iterate through the specified endstates (0 or 1) to create appropriate EquilibriumFEPTask inputs
            _logger.debug(f"\tcreating lambda state {state} EquilibriumFEPTask")
            self.thermodynamic_state.set_alchemical_parameters(float(state), lambda_protocol = LambdaProtocol(functions = self.lambda_protocol))
            input_dict = {'thermodynamic_state': copy.deepcopy(self.thermodynamic_state),
                          'nsteps_equil': n_steps_per_equilibration,
                          'topology': self.factory.hybrid_topology,
                          'n_iterations': n_equilibration_iterations,
                          'splitting': self.eq_splitting_string,
                          'atom_indices_to_save': None,
                          'trajectory_filename': None,
                          'max_size': max_size,
                          'timer': timer,
                          '_minimize': minimize,
                          'file_iterator': 0,
                          'timestep': self.timestep}


            if self.write_traj:
                _logger.debug(f"\twriting traj to {self.eq_trajectory_filename[state]}")
                equilibrium_trajectory_filename = self.eq_trajectory_filename[state]
                input_dict['trajectory_filename'] = equilibrium_trajectory_filename
            else:
                _logger.debug(f"\tnot writing traj")

            if self._eq_dict[state] == []:
                _logger.debug(f"\tself._eq_dict[{state}] is empty; initializing file_iterator at 0 ")
            else:
                last_file_num = int(self._eq_dict[state][-1][0][-7:-3])
                _logger.debug(f"\tlast file number: {last_file_num}; initiating file iterator as {last_file_num + 1}")
                file_iterator = last_file_num + 1
                input_dict['file_iterator'] = file_iterator
            task = EquilibriumFEPTask(sampler_state = self.sampler_states[state], inputs = input_dict, outputs = None)
            EquilibriumFEPTask_list.append(task)

        _logger.debug(f"scattering and mapping run_equilibrium task")
        self.activate_client(LSF = self.LSF,
                            num_processes = 2,
                            adapt = self.adapt)

        scatter_futures = self.scatter(EquilibriumFEPTask_list)
        futures = self.deploy(SequentialMonteCarlo.run_equilibrium, (scatter_futures,))
        eq_results = self.gather_results(futures)
        self.deactivate_client()

        for state, eq_result in zip(endstates, eq_results):
            _logger.debug(f"\tcomputing equilibrium task future for state = {state}")
            self._eq_dict[state].extend(eq_result.outputs['files'])
            self._eq_dict[f"{state}_reduced_potentials"].extend(eq_result.outputs['reduced_potentials'])
            self.sampler_states.update({state: eq_result.sampler_state})
            self._eq_timers[state].append(eq_result.outputs['timers'])

        _logger.debug(f"collections complete.")
        if decorrelate: # if we want to decorrelate all sample
            _logger.debug(f"decorrelating data")
            for state in endstates:
                _logger.debug(f"\tdecorrelating lambda = {state} data.")
                traj_filename = self.eq_trajectory_filename[state]
                if os.path.exists(traj_filename[:-2] + f'0000' + '.h5'):
                    _logger.debug(f"\tfound traj filename: {traj_filename[:-2] + f'0000' + '.h5'}; proceeding...")
                    [t0, g, Neff_max, A_t, uncorrelated_indices] = SequentialMonteCarlo.compute_timeseries(np.array(self._eq_dict[f"{state}_reduced_potentials"]))
                    _logger.debug(f"\tt0: {t0}; Neff_max: {Neff_max}; uncorrelated_indices: {uncorrelated_indices}")
                    self._eq_dict[f"{state}_decorrelated"] = uncorrelated_indices

                    #now we just have to turn the file tuples into an array
                    _logger.debug(f"\treorganizing decorrelated data; files w/ num_snapshots are: {self._eq_dict[state]}")
                    iterator, corrected_dict = 0, {}
                    for tupl in self._eq_dict[state]:
                        new_list = [i + iterator for i in range(tupl[1])]
                        iterator += len(new_list)
                        decorrelated_list = [i for i in new_list if i in uncorrelated_indices]
                        corrected_dict[tupl[0]] = decorrelated_list
                    self._eq_files_dict[state] = corrected_dict
                    _logger.debug(f"\t corrected_dict for state {state}: {corrected_dict}")

    @staticmethod
    def minimize(thermodynamic_state,
                 sampler_state,
                 max_iterations = 100):
        """
        Minimize the given system and state, up to a maximum number of steps.
        This does not return a copy of the samplerstate; it is simply an update-in-place.

        Arguments
        ----------
        thermodynamic_state : openmmtools.states.ThermodynamicState
            The state at which the system could be minimized
        sampler_state : openmmtools.states.SamplerState
            The starting state at which to minimize the system.
        max_iterations : int, optional, default 20
            The maximum number of minimization steps. Default is 100.

        Returns
        -------
        sampler_state : openmmtools.states.SamplerState
            The posititions and accompanying state following minimization
        """
        if type(cache.global_context_cache) == cache.DummyContextCache:
            integrator = openmm.VerletIntegrator(1.0) #we won't take any steps, so use a simple integrator
            context, integrator = cache.global_context_cache.get_context(thermodynamic_state, integrator)
            _logger.debug(f"using dummy context cache")
        else:
            _logger.debug(f"using global context cache")
            context, integrator = cache.global_context_cache.get_context(thermodynamic_state)
        sampler_state.apply_to_context(context, ignore_velocities = True)
        openmm.LocalEnergyMinimizer.minimize(context, maxIterations = max_iterations)
        sampler_state.update_from_context(context)

    @staticmethod
    def compute_timeseries(reduced_potentials):
        """
        Use pymbar timeseries to compute the uncorrelated samples in an array of reduced potentials.  Returns the uncorrelated sample indices.
        """
        from pymbar import timeseries
        t0, g, Neff_max = timeseries.detectEquilibration(reduced_potentials) #computing indices of uncorrelated timeseries
        A_t_equil = reduced_potentials[t0:]
        uncorrelated_indices = timeseries.subsampleCorrelatedData(A_t_equil, g=g)
        A_t = A_t_equil[uncorrelated_indices]
        full_uncorrelated_indices = [i+t0 for i in uncorrelated_indices]

        return [t0, g, Neff_max, A_t, full_uncorrelated_indices]

    @staticmethod
    def run_equilibrium(task):
        """
        Run n_iterations*nsteps_equil integration steps.  n_iterations mcmc moves are conducted in the initial equilibration, returning n_iterations
        reduced potentials.  This is the guess as to the burn-in time for a production.  After which, a single mcmc move of nsteps_equil
        will be conducted at a time, including a time-series (pymbar) analysis to determine whether the data are decorrelated.
        The loop will conclude when a single configuration yields an iid sample.  This will be saved.
        Parameters
        ----------
        task : FEPTask namedtuple
            The namedtuple should have an 'input' argument.  The 'input' argument is a dict characterized with at least the following keys and values:
            {
             thermodynamic_state: (<openmmtools.states.CompoundThermodynamicState>; compound thermodynamic state comprising state at lambda = 0 (1)),
             nsteps_equil: (<int>; The number of equilibrium steps that a move should make when apply is called),
             topology: (<mdtraj.Topology>; an MDTraj topology object used to construct the trajectory),
             n_iterations: (<int>; The number of times to apply the move. Note that this is not the number of steps of dynamics),
             splitting: (<str>; The splitting string for the dynamics),
             atom_indices_to_save: (<list of int, default None>; list of indices to save when excluding waters, for instance. If None, all indices are saved.),
             trajectory_filename: (<str, optional, default None>; Full filepath of trajectory files. If none, trajectory files are not written.),
             max_size: (<float>; maximum size of the trajectory numpy array allowable until it is written to disk),
             timer: (<bool, default False>; whether to time all parts of the equilibrium run),
             _minimize: (<bool, default False>; whether to minimize the sampler_state before conducting equilibration),
             file_iterator: (<int, default 0>; which index to begin writing files),
             timestep: (<unit.Quantity=float*unit.femtoseconds>; dynamical timestep)
             }
        """
        inputs = task.inputs

        timer = inputs['timer'] #bool
        timers = {}
        file_numsnapshots = []
        file_iterator = inputs['file_iterator']

        # creating copies in case computation is parallelized
        if timer: start = time.time()
        thermodynamic_state = copy.deepcopy(inputs['thermodynamic_state'])
        sampler_state = task.sampler_state
        if timer: timers['copy_state'] = time.time() - start

        if inputs['_minimize']:
            _logger.debug(f"conducting minimization")
            if timer: start = time.time()
            minimize(thermodynamic_state, sampler_state)
            if timer: timers['minimize'] = time.time() - start

        #get the atom indices we need to subset the topology and positions
        if timer: start = time.time()
        if not inputs['atom_indices_to_save']:
            atom_indices = list(range(inputs['topology'].n_atoms))
            subset_topology = inputs['topology']
        else:
            atom_indices = inputs['atom_indices_to_save']
            subset_topology = inputs['topology'].subset(atom_indices)
        if timer: timers['define_topology'] = time.time() - start

        n_atoms = subset_topology.n_atoms

        #construct the MCMove:
        mc_move = mcmc.LangevinSplittingDynamicsMove(n_steps=inputs['nsteps_equil'], splitting=inputs['splitting'], timestep = inputs['timestep'])
        mc_move.n_restart_attempts = 10

        #create a numpy array for the trajectory
        trajectory_positions, trajectory_box_lengths, trajectory_box_angles = list(), list(), list()
        reduced_potentials = list()

        #loop through iterations and apply MCMove, then collect positions into numpy array
        _logger.debug(f"conducting {inputs['n_iterations']} of production")
        if timer: eq_times = []

        init_file_iterator = inputs['file_iterator']
        for iteration in tqdm.trange(inputs['n_iterations']):
            if timer: start = time.time()
            _logger.debug(f"\tconducting iteration {iteration}")
            mc_move.apply(thermodynamic_state, sampler_state)

            #add reduced potential to reduced_potential_final_frame_list
            reduced_potentials.append(thermodynamic_state.reduced_potential(sampler_state))

            #trajectory_positions[iteration, :,:] = sampler_state.positions[atom_indices, :].value_in_unit_system(unit.md_unit_system)
            trajectory_positions.append(sampler_state.positions[atom_indices, :].value_in_unit_system(unit.md_unit_system))

            #get the box lengths and angles
            a, b, c, alpha, beta, gamma = mdtrajutils.unitcell.box_vectors_to_lengths_and_angles(*sampler_state.box_vectors)
            trajectory_box_lengths.append([a,b,c])
            trajectory_box_angles.append([alpha, beta, gamma])

            #if tajectory positions is too large, we have to write it to disk and start fresh
            if np.array(trajectory_positions).nbytes > inputs['max_size']:
                trajectory = md.Trajectory(np.array(trajectory_positions), subset_topology, unitcell_lengths=np.array(trajectory_box_lengths), unitcell_angles=np.array(trajectory_box_angles))
                if inputs['trajectory_filename'] is not None:
                    new_filename = inputs['trajectory_filename'][:-2] + f'{file_iterator:04}' + '.h5'
                    file_numsnapshots.append((new_filename, len(trajectory_positions)))
                    file_iterator +=1
                    SequentialMonteCarlo.write_equilibrium_trajectory(trajectory, new_filename)

                    #re_initialize the trajectory positions, box_lengths, box_angles
                    trajectory_positions, trajectory_box_lengths, trajectory_box_angles = list(), list(), list()

            if timer: eq_times.append(time.time() - start)

        if timer: timers['run_eq'] = eq_times
        _logger.debug(f"production done")

        #If there is a trajectory filename passed, write out the results here:
        if timer: start = time.time()
        if inputs['trajectory_filename'] is not None:
            #construct trajectory object:
            if trajectory_positions != list():
                #if it is an empty list, then the last iteration satistifed max_size and wrote the trajectory to disk;
                #in this case, we can just skip this
                trajectory = md.Trajectory(np.array(trajectory_positions), subset_topology, unitcell_lengths=np.array(trajectory_box_lengths), unitcell_angles=np.array(trajectory_box_angles))
                if file_iterator == init_file_iterator: #this means that no files have been written yet
                    new_filename = inputs['trajectory_filename'][:-2] + f'{file_iterator:04}' + '.h5'
                    file_numsnapshots.append((new_filename, len(trajectory_positions)))
                else:
                    new_filename = inputs['trajectory_filename'][:-2] + f'{file_iterator+1:04}' + '.h5'
                    file_numsnapshots.append((new_filename, len(trajectory_positions)))
                SequentialMonteCarlo.write_equilibrium_trajectory(trajectory, new_filename)

        if timer: timers['write_traj'] = time.time() - start

        if not timer:
            timers = {}

        return EquilibriumFEPTask(sampler_state = sampler_state, inputs = task.inputs, outputs = {'reduced_potentials': reduced_potentials, 'files': file_numsnapshots, 'timers': timers})

    @staticmethod
    def write_equilibrium_trajectory(trajectory: md.Trajectory, trajectory_filename: str) -> float:
        """
        Write the results of an equilibrium simulation to disk. This task will append the results to the given filename.
        Parameters
        ----------
        trajectory : md.Trajectory
            the trajectory resulting from an equilibrium simulation
        trajectory_filename : str
            the name of the trajectory file to which we should append
        Returns
        -------
        True
        """
        if not os.path.exists(trajectory_filename):
            trajectory.save_hdf5(trajectory_filename)
            _logger.debug(f"{trajectory_filename} does not exist; instantiating and writing to.")
        else:
            _logger.debug(f"{trajectory_filename} exists; appending.")
            written_traj = md.load_hdf5(trajectory_filename)
            concatenated_traj = written_traj.join(trajectory)
            concatenated_traj.save_hdf5(trajectory_filename)

        return True

    @staticmethod
    def write_nonequilibrium_trajectory(nonequilibrium_trajectory, trajectory_filename):
        """
        Write the results of a nonequilibrium switching trajectory to a file. The trajectory is written to an
        mdtraj hdf5 file.
        Parameters
        ----------
        nonequilibrium_trajectory : md.Trajectory
            The trajectory resulting from a nonequilibrium simulation
        trajectory_filename : str
            The full filepath for where to store the trajectory
        Returns
        -------
        True : bool
        """
        if nonequilibrium_trajectory is not None:
            nonequilibrium_trajectory.save_hdf5(trajectory_filename)

        return True



class LocallyOptimalAnnealing():
    """
    Actor for locally optimal annealed importance sampling.
    The initialize method will create an appropriate context and the appropriate storage objects,
    but must be called explicitly.
    """
    def initialize(self,
                   thermodynamic_state,
                   lambda_protocol = 'default',
                   timestep = 1 * unit.femtoseconds,
                   collision_rate = 1 / unit.picoseconds,
                   temperature = 300 * unit.kelvin,
                   neq_splitting_string = 'V R O R V',
                   ncmc_save_interval = None,
                   topology = None,
                   subset_atoms = None,
                   measure_shadow_work = False):

        self.context_cache = cache.global_context_cache

        if measure_shadow_work:
            measure_heat = True
        else:
            measure_heat = False

        self.thermodynamic_state = thermodynamic_state
        self.integrator = integrators.LangevinIntegrator(temperature = temperature, timestep = timestep, splitting = neq_splitting_string, measure_shadow_work = measure_shadow_work, measure_heat = measure_heat, constraint_tolerance = 1e-6, collision_rate = collision_rate)
        self.lambda_protocol_class = LambdaProtocol(functions = lambda_protocol)

        #create temperatures
        self.beta = 1.0 / (kB*temperature)
        self.temperature = temperature

        self.save_interval = ncmc_save_interval

        self.topology = topology
        self.subset_atoms = subset_atoms

        #if we have a trajectory, set up some ancillary variables:
        if self.topology is not None:
            n_atoms = self.topology.n_atoms
            self._trajectory_positions = []
            self._trajectory_box_lengths = []
            self._trajectory_box_angles = []

        #set a bool variable for pass or failure
        self.succeed = True
        return True

    def anneal(self,
               sampler_state,
               lambdas,
               noneq_trajectory_filename = None,
               num_integration_steps = 1,
               return_timer = False,
               return_sampler_state = False):
        """
        conduct annealing across lambdas.

        Arguments
        ---------
        sampler_state : openmmtools.states.SamplerState
            The starting state at which to minimize the system.
        noneq_trajectory_filename : str, default None
            Name of the nonequilibrium trajectory file to which we write
        lambdas : np.array
            numpy array of the lambdas to run
        num_integration_steps : np.array or int, default 1
            the number of integration steps to be conducted per proposal
        return_timer : bool, default False
            whether to time the annealing protocol
        return_sampler_state : bool, default False
            whether to return the last sampler state

        Returns
        -------
        incremental_work : np.array of shape (1, len(lambdas) - 1)
            cumulative works for every lambda
        sampler_state : openmmtools.states.SamplerState
            configuration at last lambda after proposal
        timer : np.array
            timers
        """
        #check if we can save the trajectory
        if noneq_trajectory_filename is not None:
            if self.save_interval is None:
                raise Exception(f"The save interval is None, but a nonequilibrium trajectory filename was given!")

        #check returnables for timers:
        if return_timer is not None:
            timer = np.zeros(len(lambdas - 1))
        else:
            timer = None

        incremental_work = np.zeros(len(lambdas - 1))
        #first set the thermodynamic state to the proper alchemical state and pull context, integrator
        self.thermodynamic_state.set_alchemical_parameters(lambdas[0], lambda_protocol = self.lambda_protocol_class)
        self.context, integrator = self.context_cache.get_context(self.thermodynamic_state, self.integrator)
        integrator.reset()
        sampler_state.apply_to_context(self.context, ignore_velocities=True)
        self.context.setVelocitiesToTemperature(self.thermodynamic_state.temperature)
        integrator.step(num_integration_steps) #we have to propagate the start state

        for idx, _lambda in enumerate(lambdas[1:]): #skip the first lambda
            try:
                if return_timer:
                    start_timer = time.time()
                incremental_work[idx] = self.compute_incremental_work(_lambda)
                integrator.step(num_integration_steps)
                if noneq_trajectory_filename is not None:
                    self.save_configuration(idx, sampler_state, context)
                if return_timer:
                    timer[idx] = time.time() - start_timer
            except Exception as e:
                print(f"failure: {e}")
                return e

        self.attempt_termination(noneq_trajectory_filename)

        #pull the last sampler state and return
        if return_sampler_state:
            sampler_state.update_from_context(self.context, ignore_velocities=True)
            return (incremental_work, sampler_state, timer)
        else:
            return (incremental_work, None, timer)



    def attempt_termination(self, noneq_trajectory_filename):
        """
        Attempt to terminate the annealing protocol and return the Particle attributes.
        """
        if noneq_trajectory_filename is not None:
            _logger.info(f"saving configuration")
            trajectory = md.Trajectory(np.array(self._trajectory_positions), self.topology, unitcell_lengths=np.array(self._trajectory_box_lengths), unitcell_angles=np.array(self._trajectory_box_angles))
            write_nonequilibrium_trajectory(trajectory, noneq_trajectory_filename)

        self._trajectory_positions = []
        self._trajectory_box_lengths = []
        self._trajectory_box_angles = []


    def compute_incremental_work(self, _lambda):
        """
        compute the incremental work of a lambda update on the thermodynamic state.
        function also updates the thermodynamic state and the context
        """
        old_rp = self.beta * self.context.getState(getEnergy=True).getPotentialEnergy()

        #update thermodynamic state and context
        self.thermodynamic_state.set_alchemical_parameters(_lambda, lambda_protocol = self.lambda_protocol_class)
        self.thermodynamic_state.apply_to_context(self.context)
        new_rp = self.beta * self.context.getState(getEnergy=True).getPotentialEnergy()
        _incremental_work = new_rp - old_rp

        return _incremental_work

    def save_configuration(self, iteration, sampler_state, context):
        """
        pass a conditional save function
        """
        if iteration % self.ncmc_save_interval == 0: #we save the protocol work if the remainder is zero
            _logger.debug(f"\t\tsaving protocol")
            #self._kinetic_energy.append(self._beta * context.getState(getEnergy=True).getKineticEnergy()) #maybe if we want kinetic energy in the future
            sampler_state.update_from_context(self.context, ignore_velocities=True) #save bandwidth by not updating the velocities

            if self.subset_atoms is None:
                self._trajectory_positions.append(sampler_state.positions[:, :].value_in_unit_system(unit.md_unit_system))
            else:
                self._trajectory_positions.append(sampler_state.positions[self.subset_atoms, :].value_in_unit_system(unit.md_unit_system))

                #get the box angles and lengths
                a, b, c, alpha, beta, gamma = mdtrajutils.unitcell.box_vectors_to_lengths_and_angles(*sampler_state.box_vectors)
                self._trajectory_box_lengths.append([a, b, c])
                self._trajectory_box_angles.append([alpha, beta, gamma])