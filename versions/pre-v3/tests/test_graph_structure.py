"""The shape of the graph is a safety property: no path from the investigation model to a write."""

from __future__ import annotations

from langgraph_production.approvals import InMemoryApprovalLedger

from billingops.investigation.graph import (
    N_CLARIFY,
    N_CONCLUDE,
    N_EXTRACT,
    N_FIXED,
    N_GATE,
    N_INVESTIGATE,
    N_PRECHECK,
    N_RUN_TOOL,
    GraphDeps,
    build_graph,
)
from billingops.llm.fake import ScriptedModel
from billingops.tools import INVESTIGATION_TOOL_SCHEMAS, READ_TOOLS


def test_nodes_and_edges() -> None:
    graph = build_graph(GraphDeps(model=ScriptedModel(), tool_runner_factory=lambda c: None, approvals=InMemoryApprovalLedger())).compile()
    g = graph.get_graph()
    nodes = set(g.nodes) - {"__start__", "__end__"}
    assert nodes == {N_EXTRACT, N_INVESTIGATE, N_RUN_TOOL, N_FIXED, N_CLARIFY, N_CONCLUDE, N_GATE, N_PRECHECK}
    edges = {(e.source, e.target) for e in g.edges}
    # Every route to precheck (the only node past the approval) passes through the gate.
    assert {s for s, t in edges if t == N_PRECHECK} == {N_GATE}
    # The investigation loop cannot reach the gate except through conclude.
    assert {s for s, t in edges if t == N_GATE} == {N_CONCLUDE, N_GATE}
    assert (N_INVESTIGATE, N_GATE) not in edges and (N_RUN_TOOL, N_GATE) not in edges
    # There is no execution node at all: the write lives outside the graph.
    assert not any("execute" in n or "refund" in n for n in nodes)


def test_no_write_tool_is_exposed_to_the_model() -> None:
    names = {schema["name"] for schema in INVESTIGATION_TOOL_SCHEMAS}
    assert not any(word in n for n in names for word in ("refund_", "issue", "create", "post", "cancel", "update"))
    assert set(READ_TOOLS) < names
    assert names - set(READ_TOOLS) == {"ask_customer", "end_investigation"}
    for schema in INVESTIGATION_TOOL_SCHEMAS:
        assert "customer_id" not in schema["input_schema"]["properties"]
        assert schema["input_schema"]["additionalProperties"] is False
