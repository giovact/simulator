import sys, os
if '..' not in sys.path:
    sys.path.append('..')

import pickle, multiprocessing, copy
import pandas as pd
import numpy as np
from collections import namedtuple, defaultdict
import botorch.utils.transforms as transforms
from lib.calibrationFunctions import (
    pdict_to_parr, parr_to_pdict, save_state, load_state, 
    get_calibrated_params, gen_initial_seeds, get_test_capacity, downsample_cases)
from lib.mobilitysim import MobilitySimulator
from lib.parallel import launch_parallel_simulations
from lib.distributions import CovidDistributions
from lib.data import collect_data_from_df
from lib.measures import *
from lib.calibrationSettings import (
    calibration_lockdown_dates, 
    calibration_testing_params, 
    calibration_lockdown_beta_multipliers,
    calibration_mob_paths)

TO_HOURS = 24.0
ROOT = 'summaries'

"""Tuples representing various objects concerning a simulation and experiment"""

Simulation = namedtuple('Simulation', (

    # Generic information
    'experiment_info',   # Description of the experiment that contains the simulation
    'simulation_info',   # Description of the simulation itself
    'start_date',        # Start date
    'end_date',          # End date
    'sim_days',          # Days of simulation
    'country',           # Country
    'area',              # Area
    'random_repeats',    # Random repeats of simulation

    # Mobility and measures
    'mob_settings_file', # Mobility settings
    'full_scale',        # Whether or not simulation is done at full scale
    'measure_list',      # Measure list
    'testing_params',    # Testing params

    # Model
    'model_params',      # Model parameters (from calibration)
    'distributions',     # Transition distributions
    'initial_seeds',     # Simulation seeds

))

Result = namedtuple('Result', (
    'metadata',    # metadata of simulation that was run, here a `Simulation` namedtuple
    'summary',     # result summary of simulation
))

Plot = namedtuple('Plot', (
    'path',    # path to result file of this simulation containing pickled `Result` namedetuple
    'label',   # label of this plot on the legend
    'ymax',    # ymax of this plot
))


"""Helper functions"""

def get_properties(objs, property):
    '''Retrieves list of properties for list of namedtuples'''
    out = []
    for o in objs:
        if isinstance(o, dict):
            out.append(o[property])
        elif isinstance(o, Simulation) or isinstance(o, Plot) or isinstance(o, Result):
            out.append(getattr(o, property))
        else:
            raise ValueError('Unknown type of elements in `objs`.')
    return out


def save_summary(obj, path):
    '''Saves summary file'''
    with open(os.path.join(ROOT, path), 'wb') as fp:
        pickle.dump(obj, fp)

def load_summary(path):
    '''Loads summary file'''
    with open(os.path.join(ROOT, path), 'rb') as fp:
        obj = pickle.load(fp)
    return obj

def load_summary_list(paths):
    '''Loads list of several summaries'''
    objs = []
    for p in paths:
        try:
            objs.append(load_summary(p))
        except FileNotFoundError:
            print(f'{p} not found.')
    return objs

def options_to_str(**options):
        return '-'.join(['{}={}'.format(k, v) for k, v in options.items()])


"""Experiment class for structured experimentation with simulations"""

class Experiment(object):
    """
    Class to organize a set of experiment simulations. One experiment objects
    contains several simulations that are stored and can be analyzed collectively. 
    """

    def __init__(self, *, 
        experiment_info,
        start_date,
        end_date,
        random_repeats,
        full_scale,
        verbose,
        multi_beta_calibration=False):

        self.experiment_info = experiment_info
        self.start_date = start_date
        self.end_date = end_date
        self.random_repeats = random_repeats
        self.full_scale = full_scale
        self.multi_beta_calibration = multi_beta_calibration
        self.verbose = verbose

        # list simulations of experiment
        self.sims = []

    def get_sim_path(self, sim):
        return sim.experiment_info + '/' + sim.experiment_info + '-' + sim.simulation_info

    def save_run(self, sim, summary):
        filename = self.get_sim_path(sim) + '.pk'
        obj = Result(
            metadata=sim,
            summary=summary,
        )
        with open(os.path.join(ROOT, filename), 'wb') as fp:
            pickle.dump(obj, fp)
        return

    def add(self, *,
        simulation_info,
        country,
        area,        
        measure_list,
        full_scale,
        test_update=None,
        seed_summary_path=None,
        set_calibrated_params_to=None,
        set_initial_seeds_to=None):

        # Set time window based on experiment start and end date
        sim_days = (pd.to_datetime(self.end_date) - pd.to_datetime(self.start_date)).days
        max_time = TO_HOURS * sim_days  # in hours

         # extract lockdown period
        lockdown_start_date = pd.to_datetime(
            calibration_lockdown_dates[country]['start'])
        lockdown_end_date = pd.to_datetime(
            calibration_lockdown_dates[country]['end'])

        days_until_lockdown_start = (lockdown_start_date - pd.to_datetime(self.start_date)).days
        days_until_lockdown_end = (lockdown_end_date - pd.to_datetime(self.start_date)).days

        # Load mob settings
        mob_settings_file = calibration_mob_paths[country][area][1 if full_scale else 0]
        with open(mob_settings_file, 'rb') as fp:
            mob_settings = pickle.load(fp)

        # Obtain COVID19 case date for country and area to estimate testing capacity and heuristic seeds if necessary
        unscaled_area_cases = collect_data_from_df(country=country, area=area, datatype='new',
                                                start_date_string=self.start_date, end_date_string=self.end_date)
        assert(len(unscaled_area_cases.shape) == 2)

        # Scale down cases based on number of people in town and region
        sim_cases = downsample_cases(unscaled_area_cases, mob_settings)

        # Instantiate correct state transition distributions (estimated from literature)
        distributions = CovidDistributions(country=country)

        # Get initial seeds for simulation
        # (a) Define heuristically based on true cases and literature distribution estimates
        if seed_summary_path is None:

            # Generate initial seeds based on unscaled case numbers in town
            initial_seeds = gen_initial_seeds(
                sim_cases, day=0)

            if sum(initial_seeds.values()) == 0:
                print('No states seeded at start time; cannot start simulation.\n'
                    'Consider setting a later start date for calibration using the "--start" flag.')
                sys.exit(0)

        # (b) Define based state of previous batch of simulations,
        # using the random rollout that best matched the true cases in terms of squared error
        else:
            seed_summary_ = load_summary(seed_summary_path)
            seed_day_ = seed_summary_.max_time # take seeds at the end of simulation
            initial_seeds = extract_seeds_from_summary(
                seed_summary_, seed_day_, sim_cases)

        initial_seeds = set_initial_seeds_to or initial_seeds

        # Load calibrated model parameters for this area
        raw_calibrated_params = get_calibrated_params(
            country=country, area=area, multi_beta_calibration=self.multi_beta_calibration)
        calibrated_params = set_calibrated_params_to or raw_calibrated_params

        p_stay_home_calibrated = raw_calibrated_params['p_stay_home']

        if self.multi_beta_calibration:
            betas = calibrated_params['betas']
        else:
            betas = {
                'education': calibrated_params['beta_site'],
                'social': calibrated_params['beta_site'],
                'bus_stop': calibrated_params['beta_site'],
                'office': calibrated_params['beta_site'],
                'supermarket': calibrated_params['beta_site'],
            }

        model_params = {
            'betas': betas,
            'beta_household': calibrated_params['beta_household'],
        }        

        # Add standard measure of positives staying isolated 
        measure_list += [
            # standard behavior of positively tested: full isolation
            SocialDistancingForPositiveMeasure(
                t_window=Interval(0.0, max_time), p_stay_home=1.0),
            SocialDistancingForPositiveMeasureHousehold(
                t_window=Interval(0.0, max_time), p_isolate=1.0),
        ]

        # Add standard measures if simulation is happening during lockdown
        measure_list += [

            # social distancing factor during lockdown: calibrated
            SocialDistancingForAllMeasure(
                t_window=Interval(TO_HOURS * days_until_lockdown_start,
                                TO_HOURS * days_until_lockdown_end),
                p_stay_home=p_stay_home_calibrated),

            # site specific measures: fixed in advance, outside of calibration
            BetaMultiplierMeasureByType(
                t_window=Interval(TO_HOURS * days_until_lockdown_start,
                                TO_HOURS * days_until_lockdown_end),
                beta_multiplier=calibration_lockdown_beta_multipliers)
        ]

        measure_list = MeasureList(measure_list)

        # Set testing conditions
        scaled_test_capacity = get_test_capacity(
            country, area, mob_settings, end_date_string=self.end_date)
        testing_params = copy.deepcopy(calibration_testing_params)
        testing_params['tests_per_batch'] = scaled_test_capacity
        testing_params['testing_t_window'] = [0.0, max_time]
        if test_update:
            testing_params = test_update(testing_params)

        # store simulation
        self.sims.append(Simulation(
            # Generic information
            experiment_info=self.experiment_info,
            simulation_info=simulation_info,
            start_date=self.start_date,
            end_date=self.end_date,
            sim_days=sim_days,
            country=country,
            area=area,
            random_repeats=self.random_repeats,

            # Mobility and measures
            mob_settings_file=mob_settings_file,
            full_scale=full_scale,
            measure_list=measure_list,
            testing_params=testing_params,

            # Model
            model_params=model_params,
            distributions=distributions,
            initial_seeds=initial_seeds,
        ))

        if self.verbose:
            print(f'Added {self.get_sim_path(self.sims[-1])}')


    def run_all(self):

        '''
        Runs all simulations that were provided via the `add` method and stored in `self.sims`
        '''

        # generate experiment folder
        current_directory = os.getcwd()
        directory = os.path.join(current_directory, ROOT, self.experiment_info)        
        if not os.path.exists(directory):
            os.makedirs(directory)
        
        # run all simulations
        for sim in self.sims:

            with open(sim.mob_settings_file, 'rb') as fp:
                mob_settings = pickle.load(fp)
        
            summary = launch_parallel_simulations(
                mob_settings=sim.mob_settings_file,
                distributions=sim.distributions,
                random_repeats=sim.random_repeats,
                cpu_count=multiprocessing.cpu_count(),
                params=sim.model_params,
                initial_seeds=sim.initial_seeds,
                testing_params=sim.testing_params,
                measure_list=sim.measure_list,
                max_time=TO_HOURS * sim.sim_days,
                home_loc=mob_settings['home_loc'],
                num_people=len(mob_settings['home_loc']),
                site_loc=mob_settings['site_loc'],
                num_sites=len(mob_settings['site_loc']),
                lazy_contacts=True,
                verbose=False)

            self.save_run(sim, summary)
                 
            if self.verbose:
                print('Finished ' + self.get_sim_path(sim))

            
            
