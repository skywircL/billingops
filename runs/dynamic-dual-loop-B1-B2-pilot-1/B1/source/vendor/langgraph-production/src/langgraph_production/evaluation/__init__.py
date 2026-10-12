"""The part that turns "the agent works" into a number a build can fail on.

Three measurements, each answering a question a client actually asks:

- **Routing** (:mod:`.routing`): does it send tickets to the right branch, and
  when it is wrong, wrong in which direction? Precision, recall, and a
  confusion matrix per branch.
- **Interrupts** (:mod:`.interrupts`): does it stop when it must, and does it
  stop when it must not? A stop that was not needed is counted as a failure,
  not as caution. Approval fatigue is how human-in-the-loop dies in the field.
- **Resume** (:mod:`.resume`): kill the process at every boundary, bring it
  back, and check the final state matches and the effect ran once.

:mod:`.report` applies thresholds and returns an exit code, which is what the
CI workflow runs.
"""

from .fixtures import TicketFixture, load_fixtures
from .interrupts import InterruptReport, evaluate_interrupts
from .report import EvalReport, Thresholds, load_thresholds, run_evaluation
from .resume import ResumeReport, evaluate_resume
from .routing import BranchMetrics, RoutingReport, evaluate_routing

__all__ = [
    "BranchMetrics",
    "EvalReport",
    "InterruptReport",
    "ResumeReport",
    "RoutingReport",
    "Thresholds",
    "TicketFixture",
    "evaluate_interrupts",
    "evaluate_resume",
    "evaluate_routing",
    "load_fixtures",
    "load_thresholds",
    "run_evaluation",
]
