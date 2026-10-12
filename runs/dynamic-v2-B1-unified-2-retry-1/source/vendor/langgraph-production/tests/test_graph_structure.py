"""The shape of the compiled graph, asserted so the README diagram cannot rot.

The diagram is not compared to a copy of itself. The mermaid block is read out
of README.md, parsed, and checked against the compiled graph, so editing one
without the other fails here rather than in a reader's head six months later.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from langgraph_production.runtime import SupportRuntime

README = Path(__file__).resolve().parents[1] / "README.md"

MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
LEADING_IDENTIFIER = re.compile(r"^([A-Za-z_]\w*)")
EDGE_LABEL = re.compile(r"^\s*\|([^|]*)\|")

DIAGRAM_ALIASES = {"START": "__start__", "STOP": "__end__"}
"""The two boxes drawn for humans. LangGraph names them differently."""

EXPECTED_NODES = {"__start__", "classify", "plan", "human_gate", "act", "compose", "__end__"}

EXPECTED_EDGES = {
    ("__start__", "classify", False),
    ("classify", "plan", True),
    ("classify", "human_gate", True),
    ("classify", "compose", True),
    ("plan", "act", True),
    ("plan", "human_gate", True),
    ("human_gate", "act", True),
    ("human_gate", "compose", True),
    ("act", "compose", False),
    ("compose", "__end__", False),
}


def edges(runtime: SupportRuntime) -> set[tuple[str, str, bool]]:
    return {
        (edge.source, edge.target, bool(edge.conditional))
        for edge in runtime.graph.get_graph().edges
    }


def _named(raw: str) -> str:
    return DIAGRAM_ALIASES.get(raw, raw)


def parse_diagram(markdown: str) -> tuple[set[str], set[tuple[str, str, bool]]]:
    """Read the nodes and edges out of the README's mermaid block.

    An edge carrying a ``|label|`` is a conditional one, which is how the
    diagram draws a routing decision, so the label is compared too rather than
    treated as decoration.
    """
    match = MERMAID_BLOCK.search(markdown)
    if match is None:
        raise AssertionError("README.md has no mermaid block to check the graph against")

    nodes: set[str] = set()
    found: set[tuple[str, str, bool]] = set()
    for line in match.group(1).splitlines():
        text = line.strip()
        if not text or text.startswith(("graph ", "%%")):
            continue

        left, arrow, right = text.partition("-->")
        head = LEADING_IDENTIFIER.match(left.strip())
        if head is None:
            continue
        source = _named(head.group(1))
        nodes.add(source)
        if not arrow:
            continue

        label = EDGE_LABEL.match(right)
        remainder = right[label.end() :] if label else right
        tail = IDENTIFIER.search(remainder)
        if tail is None:
            raise AssertionError(f"edge without a target in the diagram: {text}")
        target = _named(tail.group(0))
        nodes.add(target)
        found.add((source, target, label is not None))
    return nodes, found


@pytest.fixture(scope="module")
def diagram() -> tuple[set[str], set[tuple[str, str, bool]]]:
    return parse_diagram(README.read_text(encoding="utf-8"))


def test_the_graph_has_exactly_the_documented_nodes(runtime: SupportRuntime) -> None:
    assert set(runtime.graph.get_graph().nodes) == EXPECTED_NODES


def test_the_graph_has_exactly_the_documented_edges(runtime: SupportRuntime) -> None:
    assert edges(runtime) == EXPECTED_EDGES


def test_only_two_nodes_lead_into_act(runtime: SupportRuntime) -> None:
    """``plan`` for a read only tool, ``human_gate`` for anything else."""
    sources = {source for source, target, _ in edges(runtime) if target == "act"}
    assert sources == {"plan", "human_gate"}


def test_act_leads_only_to_compose(runtime: SupportRuntime) -> None:
    targets = {target for source, target, _ in edges(runtime) if source == "act"}
    assert targets == {"compose"}


def test_compose_is_the_only_way_out(runtime: SupportRuntime) -> None:
    sources = {source for source, target, _ in edges(runtime) if target == "__end__"}
    assert sources == {"compose"}


def test_the_classify_node_is_the_only_entry_point(runtime: SupportRuntime) -> None:
    targets = {target for source, target, _ in edges(runtime) if source == "__start__"}
    assert targets == {"classify"}


def test_the_graph_can_draw_itself(runtime: SupportRuntime) -> None:
    drawn = runtime.graph.get_graph().draw_mermaid()
    for node in ("classify", "plan", "human_gate", "act", "compose"):
        assert node in drawn


def test_the_readme_diagram_has_the_nodes_the_graph_has(
    runtime: SupportRuntime, diagram: tuple[set[str], set[tuple[str, str, bool]]]
) -> None:
    nodes, _ = diagram
    assert nodes == set(runtime.graph.get_graph().nodes)


def test_the_readme_diagram_has_the_edges_the_graph_has(
    runtime: SupportRuntime, diagram: tuple[set[str], set[tuple[str, str, bool]]]
) -> None:
    """Including which ones are conditional: a labelled arrow is a decision."""
    _, drawn_edges = diagram
    assert drawn_edges == edges(runtime)


def test_the_diagram_parser_notices_an_edited_diagram() -> None:
    """The check is only worth having if a wrong diagram actually fails it."""
    tampered = "```mermaid\ngraph TD\n    act -->|sneaky| compose\n```"
    nodes, drawn_edges = parse_diagram(tampered)
    assert nodes == {"act", "compose"}
    assert drawn_edges == {("act", "compose", True)}
    assert drawn_edges != {("act", "compose", False)}


def test_a_file_without_a_diagram_is_an_error() -> None:
    with pytest.raises(AssertionError, match="no mermaid block"):
        parse_diagram("# no diagram here")
