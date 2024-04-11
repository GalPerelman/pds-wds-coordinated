import gurobipy.gurobipy
import numpy as np
import pandas as pd

import lp
import utils
from pds import PDS
from lp import Optimizer, WDS

pd.set_option('display.max_columns', 500)
pd.set_option('display.width', 1000)


class Scenario:
    def __init__(self, n_tanks: int, n_batteries: int, power_lines: list, max_outage_lines: int, **kwargs):
        self.n_tanks = n_tanks
        self.n_batteries = n_batteries
        self.power_lines = power_lines
        self.max_outage_lines = max_outage_lines
        self.kwargs = kwargs

        # default parameters - standard scenario
        self.t = 24
        self.start_time = 0
        self.wds_demand_factor = 1
        self.pds_demand_factor = 1
        self.pv_factor = 1
        self.outage_lines = []
        self.tanks_state = np.ones(shape=(self.n_tanks,))
        self.batteries_state = np.ones(shape=(self.n_batteries,))

        # set specified values if passed:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def draw_random(self):
        """
        Overwrite defaults but maintain specified values that were passed to class initiation
        """
        outage_set = utils.get_subsets_of_max_size(elements=self.power_lines, max_subset_size=self.max_outage_lines)
        rand_params = {
            "t": np.random.randint(low=6, high=25),
            "start_time": np.random.randint(low=0, high=24),
            "wds_demand_factor": np.random.uniform(low=0.8, high=1.2),
            "pds_demand_factor": np.random.uniform(low=0.75, high=1.25),
            "pv_factor": np.random.uniform(low=0.8, high=1.2),
            "outage_lines": outage_set[np.random.randint(low=0, high=len(outage_set))],
            "tanks_state": np.random.uniform(low=0.2, high=1, size=self.n_tanks),
            "batteries_state": np.random.uniform(low=0.2, high=1, size=self.n_batteries),
        }

        for param, rand_value in rand_params.items():
            setattr(self, param, self.kwargs.get(param, rand_params[param]))


class CommunicateProtocolMaxInfo:
    """
    In this communication protocol, the power utility has access to WDS optimization models
    The power utility can solve a conjunctive problem and deliver pumps penalties based on it
    """

    def __init__(self, pds_data, wds_data, scenario):
        self.pds_data = pds_data
        self.wds_data = wds_data
        self.scenario = scenario

    def get_pumps_penalties(self):
        # run cooperative - assuming max information sharing, power utility can run cooperative model
        model = opt_resilience(pds_data=self.pds_data, wds_data=self.wds_data, scenario=self.scenario, display=False)
        # get pumps schedule
        pumps_penalties = model.wds.pumps_combs @ model.x['pumps'].get()
        pumps_penalties = np.ones(pumps_penalties.shape) - pumps_penalties
        return pumps_penalties
    
    
class CommunicateProtocolBasic:
    """
        In this communication protocol, the power utility has only the standard pumps schedule
        Standard pumps schedule - was delivered as plan from water utility or estimated based on historic pumping loads
        The power utility solves an inner optimization problem to evaluate the pumps penalties
        """

    def __init__(self, pds_data, wds_data, scenario):
        self.pds_data = pds_data
        self.wds_data = wds_data
        self.scenario = scenario

    def get_wds_standard(self):
        """
        wds standard schedule - based on wds cost minimization
        """
        try:
            model_wds = lp.Optimizer(self.pds_data, self.wds_data, scenario=self.scenario, display=False)
            model_wds.build_water_problem(obj="cost", final_tanks_ratio=1, w=1)
            model_wds.solve()
            return model_wds.x['pumps'].get()
        except RuntimeError:
            return

    def get_pumps_penalties(self):
        # get the standard wds operation
        x_pumps = self.get_wds_standard()
        if x_pumps is None:
            return

        else:
            # solve inner pds problem
            model = lp.Optimizer(pds_data="data/pds_emergency_futurized", wds_data="data/wds_wells", scenario=self.scenario,
                                 display=False)
            model.build_inner_pds_problem(x_pumps=x_pumps)
            for line_idx in self.scenario.outage_lines:
                model.disable_power_line(line_idx)
            model.solve()

            pumps_penalties = model.wds.pumps_combs @ model.x['pumps'].get()
            pumps_penalties = np.ones(pumps_penalties.shape) - pumps_penalties
            return pumps_penalties


class Simulation:
    def __init__(self, pds_data, wds_data, opt_display, final_tanks_ratio, comm_protocol, rand_scenario,
                 scenario_const=None):

        self.pds_data = pds_data
        self.wds_data = wds_data
        self.opt_display = opt_display
        self.final_tanks_ratio = final_tanks_ratio
        self.comm_protocol = comm_protocol
        self.scenario_const = scenario_const

        # initiate pds and wds objects for data usage in simulation functions
        self.base_pds = PDS(self.pds_data)
        self.base_wds = WDS(self.wds_data)
        self.scenario = self.draw_scenario()

    def draw_scenario(self):
        s = Scenario(n_tanks=self.base_wds.n_tanks,
                     n_batteries=self.base_pds.n_batteries,
                     power_lines=self.base_pds.lines.index.to_list(),
                     max_outage_lines=2,
                     **self.scenario_const
                     )

        s.draw_random()
        return s

    def run_individual(self, wds_objective):
        model_wds = Optimizer(pds_data=self.pds_data, wds_data=self.wds_data, scenario=self.scenario,
                              display=self.opt_display)
        model_wds.build_water_problem(obj=wds_objective)
        model_wds.solve()
        if model_wds.status == gurobipy.gurobipy.GRB.INFEASIBLE or model_wds.status == gurobipy.gurobipy.GRB.INF_OR_UNBD:
            model = Optimizer(pds_data=self.pds_data, wds_data=self.wds_data, scenario=self.scenario,
                              display=self.opt_display)

            model.objective = None
            model.status = gurobipy.gurobipy.GRB.INFEASIBLE

        else:
            x_pumps = model_wds.x['pumps'].get()  # planned schedule (can be seen also as historic nominal schedule)

            # Non-cooperative - solve resilience problem with given WDS operation
            model = Optimizer(pds_data=self.pds_data, wds_data=self.wds_data, scenario=self.scenario,
                              display=self.opt_display)
            for line_idx in self.scenario.outage_lines:
                model.disable_power_line(line_idx)
            model.build_combined_resilience_problem(x_pumps=x_pumps)
            model.solve()

        return model

    def run_cooperated(self):
        model = Optimizer(pds_data=self.pds_data, wds_data=self.wds_data, scenario=self.scenario,
                          display=self.opt_display)
        for line_idx in self.scenario.outage_lines:
            model.disable_power_line(line_idx)
        model.build_combined_resilience_problem()
        model.solve()
        return model

    def run_communicate(self, comm_protocol):
        p = comm_protocol(self.pds_data, self.wds_data, self.scenario)
        w = p.get_pumps_penalties()
        if w is None:
            model = opt_resilience(self.pds_data, self.wds_data, self.scenario, self.opt_display)
            model.objective = None
            model.status = gurobipy.gurobipy.GRB.INFEASIBLE

        else:
            model_wds = Optimizer(self.pds_data, self.wds_data, scenario=self.scenario, display=False)
            model_wds.build_water_problem(obj="emergency", final_tanks_ratio=self.final_tanks_ratio, w=w)
            model_wds.solve()
            x_pumps = model_wds.x['pumps'].get()  # planned schedule (can be seen also as historic nominal schedule)
            model = opt_resilience(self.pds_data, self.wds_data, self.scenario, self.opt_display, x_pumps=x_pumps)

        return model

    def run_and_record(self):
        joint_model = self.run_cooperated()

        # run realistic situation where systems not communicate at all
        independent_model = self.run_individual(wds_objective="cost")

        # wds based on information from pds
        communicate_model = self.run_communicate(self.comm_protocol)

        return {
            "cooperative": joint_model.objective,
            "independent": independent_model.objective,
            "communicate": communicate_model.objective,
            "t": self.scenario.t,
            "start_time": self.scenario.start_time,
            "wds_demand_factor": self.scenario.wds_demand_factor,
            "pds_demand_factor": self.scenario.pds_demand_factor,
            "pv_factor": self.scenario.pv_factor,
            "outage_lines": self.scenario.outage_lines,
            "tanks_states": self.scenario.tanks_state,
            "batteries_state": self.scenario.batteries_state
        }


def opt_resilience(pds_data, wds_data, scenario, display, x_pumps=None):
    model = Optimizer(pds_data=pds_data, wds_data=wds_data, scenario=scenario, display=display)
    for line_idx in scenario.outage_lines:
        model.disable_power_line(line_idx)
    model.build_combined_resilience_problem(x_pumps=x_pumps)
    model.solve()
    return model


def run_n_scenarios(n):
    results = []
    for _ in range(n):
        sim = Simulation(pds_data="data/pds_emergency_futurized", wds_data="data/wds_wells", opt_display=False,
                         comm_protocol=CommunicateProtocolBasic)

        temp = sim.run_and_record()
        temp = utils.convert_arrays_to_lists(temp)
        results.append(temp)

        pd.DataFrame(results).to_csv("sim.csv")


if __name__ == "__main__":
    pds_data = "data/pds_emergency_futurized"
    wds_data = "data/wds_wells"

    pds = PDS(pds_data)
    wds = WDS(wds_data)

    run_n_scenarios(n=500)




