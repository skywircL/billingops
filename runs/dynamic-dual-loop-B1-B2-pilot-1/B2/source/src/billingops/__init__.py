"""BillingOps Agent.

A billing investigation agent built on the reliability layer from
``langgraph-production`` (vendored, pinned in ``vendor/UPSTREAM_PIN.txt``).
The upstream contribution is checkpointing, the single-use approval ledger,
the effect ledger and the crash-injection hooks; everything under this package
is the billing domain, the investigation model protocol, persistent commands,
HTTP execution with independent verification, evaluation and observability.
"""

__version__ = "0.1.0"
