"""Replayable synthetic evaluation harness; it makes no claim about real crew performance."""
from collections.abc import Callable
from dataclasses import dataclass

from ..models import ExecutionPlan


@dataclass(frozen=True)
class Scenario: id: str; expected_plan: str; rupture: bool = False
@dataclass
class Result: scenario_id: str; task_success: bool; latency_ms: float; token_use: int; plan_regret: float; disagreement: bool; false_consensus: bool; coordination_overhead: float
def s_net(results: list[Result], best_baseline_success: float) -> float:
    if not results: return 0.0
    return sum(r.task_success for r in results)/len(results) - best_baseline_success - sum(r.coordination_overhead for r in results)/len(results)
def replay(scenario: Scenario, choose: Callable[[Scenario], ExecutionPlan]) -> Result:
    plan = choose(scenario)
    success = plan.plan_id == scenario.expected_plan
    return Result(scenario.id,success,1.0,0,0.0 if success else 1.0,scenario.rupture,False,0.1 if scenario.rupture else 0.0)
