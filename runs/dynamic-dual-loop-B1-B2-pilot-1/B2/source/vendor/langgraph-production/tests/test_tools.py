from __future__ import annotations

import pytest

from langgraph_production.tools import ToolRegistry, UnknownRecord, validation_errors


def test_registry_exposes_every_tool(registry: ToolRegistry) -> None:
    assert set(registry.names) == {
        "lookup_order_status",
        "lookup_invoice",
        "check_service_status",
        "issue_refund",
        "cancel_subscription",
        "reset_credentials",
    }


def test_exactly_three_tools_are_irreversible(registry: ToolRegistry) -> None:
    assert set(registry.irreversible_names) == {
        "issue_refund",
        "cancel_subscription",
        "reset_credentials",
    }


@pytest.mark.parametrize("name", ["lookup_order_status", "lookup_invoice", "check_service_status"])
def test_read_only_tools_are_not_flagged_irreversible(registry: ToolRegistry, name: str) -> None:
    assert registry.is_irreversible(name) is False


def test_require_raises_for_an_unknown_tool(registry: ToolRegistry) -> None:
    with pytest.raises(KeyError):
        registry.require("delete_everything")


def test_catalogue_marks_the_irreversible_tools(registry: ToolRegistry) -> None:
    catalogue = registry.catalogue()
    assert "issue_refund" in catalogue
    assert catalogue.count("[IRREVERSIBLE, needs human approval]") == 3


def test_catalogue_lists_argument_names(registry: ToolRegistry) -> None:
    assert "invoice_ref" in registry.catalogue()


def test_order_lookup_returns_the_record(registry: ToolRegistry) -> None:
    result = registry.require("lookup_order_status").run({"order_ref": "ORD-40021"})
    assert result["status"] == "in_transit"


def test_refund_reports_the_remaining_balance(registry: ToolRegistry) -> None:
    result = registry.require("issue_refund").run(
        {"invoice_ref": "INV-10032", "amount_cents": 4500}
    )
    assert result["refunded_cents"] == 4500
    assert result["remaining_cents"] == 4500


def test_a_well_formed_but_unknown_reference_raises(registry: ToolRegistry) -> None:
    with pytest.raises(UnknownRecord):
        registry.require("lookup_invoice").run({"invoice_ref": "INV-99999"})


@pytest.mark.parametrize(
    "tool,args",
    [
        ("lookup_order_status", {"order_ref": "40021"}),
        ("lookup_order_status", {"order_ref": "ORD-4002"}),
        ("lookup_invoice", {"invoice_ref": "inv-10032"}),
        ("check_service_status", {"region": "eu-central"}),
        ("issue_refund", {"invoice_ref": "INV-10032", "amount_cents": 0}),
        ("issue_refund", {"invoice_ref": "INV-10032", "amount_cents": -100}),
        ("cancel_subscription", {"account_ref": "ACC-20411"}),
    ],
)
def test_invalid_arguments_are_rejected_before_any_handler_runs(
    registry: ToolRegistry, tool: str, args: dict[str, object]
) -> None:
    assert validation_errors(registry.require(tool), args) is not None


def test_missing_arguments_are_rejected(registry: ToolRegistry) -> None:
    assert validation_errors(registry.require("issue_refund"), {"invoice_ref": "INV-10032"})


def test_extra_arguments_are_rejected(registry: ToolRegistry) -> None:
    problem = validation_errors(
        registry.require("cancel_subscription"),
        {"account_ref": "ACC-2041", "force": True},
    )
    assert problem is not None


def test_valid_arguments_report_no_problem(registry: ToolRegistry) -> None:
    assert validation_errors(registry.require("cancel_subscription"), {"account_ref": "ACC-2041"}) is None


def test_a_registry_can_be_narrowed(registry: ToolRegistry) -> None:
    read_only = ToolRegistry(
        tuple(registry.require(name) for name in ("lookup_invoice", "lookup_order_status"))
    )
    assert read_only.irreversible_names == ()
    assert "issue_refund" not in read_only
