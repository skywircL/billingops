"""The versioned evaluation task contract (``EvalTask``).

One task = one initial Billing world + one customer message + the scripts the
simulators follow + what the business spec says must and must not happen.
The contract is documented for task authors in ``docs/EVAL_TASK_CONTRACT.md``;
this module is the executable half: validation, lineage checks, the legacy
``Case`` adapter and dataset freezing.

Nothing here knows how the agent works. Expected facts are stated as
*information* (``invoice_owned``, ``refunds_of_payment``) and resolved to the
tool reads that can establish them by the behaviour scorer, so a task never
prescribes a tool sequence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

CONTRACT_VERSION = "1"

BUSINESS_LINES = ("duplicate", "refund_status", "subscription", "refund_policy", "out_of_scope")  # last two: v1.2
LANGUAGES = ("zh", "en", "mixed")
DISCLOSURES = ("full", "partial", "none", "wrong")
RISKS = ("write", "read_only", "permission", "fault", "approval")
CAPABILITIES = tuple(f"C{i}" for i in range(1, 10))
EXPOSURES = ("sealed", "dev", "exposed")
REVIEW_STATUSES = ("machine_written", "rule_validated", "human_reviewed")

CONCLUSIONS = (
    "refund_proposed", "no_refund_due", "no_duplicate_found", "explain_refund_status",
    "explain_subscription", "escalate", "insufficient_information",
    # v1.2
    "explain_refund_policy", "explain_out_of_scope", "refund_withdrawn",
)
FOLLOWUP_KINDS = ("new_info", "withdraw", "progress", "unclear")
FINAL_STATES = ("closed", "escalated", "uncertain", "error")
MECHANISM_RE = re.compile(r"^(?:[DSPXERO]-\d{2}|NEW:[a-z0-9_-]{3,40})$")
# Boundaries are ASCII alphanumerics, not ``\b``: Chinese text often writes an id with no space next to a CJK
# character, which ``\b`` treats as part of the word ("账单INV-2026-0142扣了两次").
ID_RE = re.compile(r"(?<![A-Za-z0-9])(?:INV|PAY|REF|SUB)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")

# Information a task can require. Each fact names the ids it is about; the
# behaviour scorer knows which successful reads establish it.
FACT_KINDS = {
    "invoice_owned": ("invoice_id",),
    "payments_of_invoice": ("invoice_id",),
    "payment_record": ("payment_id",),
    "refunds_of_payment": ("payment_id",),
    "period_siblings": ("invoice_id",),
    "all_invoices_listed": (),
    "customer_refunds": (),
    "subscription_history": (),
    "policy": ("name",),
}

FAULT_KEYS = ("lost_response_after_commit", "verification_unavailable", "delay_before_commit_ms", "delay_before_commit_uses", "reads_unavailable",
              "write_unavailable")  # write_unavailable: coverage-v1, a refund write refused before anything is written
FOLLOWUP_STATES = ("awaiting_approval", "uncertain", "escalated", "error")
FOLLOWUP_COMMANDS = ("message", "resume", "escalate", "billing_patch")
"""``billing_patch`` (coverage-v1): a third party changes Billing while the proposal waits for approval (spec 5.2: the
pre-execution recheck must notice). The patch only adds refund rows; the scorer counts them as part of the starting
world, so they are never attributed to the agent."""
EXECUTION_STATUSES = ("done", "in_doubt", "confirmed_not_executed")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


_OPTIONAL_TASK_FIELDS = ("reference_followups",)
_OPTIONAL_EXPECTED_FIELDS = ("max_proposal_version", "min_proposal_version", "progress_replies", "execution_status_any")


@dataclass
class Expected:
    """What the business spec allows for this task. Every field is checked by a scorer layer."""

    outcomes_any: list[dict[str, str]]
    """Allowed ``{"conclusion": ..., "final_state": ...}`` pairs (spec §7.4 equivalences)."""
    target_payment_id: str | None = None
    amount_cents: int | None = None
    refund_delta: dict[str, Any] | None = None
    """Expected new refund on the target: ``{"payment_id", "amount_cents", "count": 1}``; ``None`` = no money moves."""
    required_facts_any: list[list[dict[str, Any]]] = field(default_factory=list)
    forbidden_reads: list[str] = field(default_factory=list)
    """Ids that must never be read successfully (other customers' records)."""
    must_clarify: bool | None = None
    """``True`` = a question to the customer is required; ``False`` = asking is a failure; ``None`` = either."""
    verdict_reason: str | None = None
    response_rubric: list[dict[str, str]] = field(default_factory=list)
    """Atomic claims for the response layer: ``{"id", "claim", "polarity": "must"|"must_not"}``."""
    max_proposal_version: int | None = None
    """v1.2: the case must end at or below this proposal version (a progress question must not reopen the proposal)."""
    min_proposal_version: int | None = None
    """v1.2: the case must reach at least this proposal version (new or unclear information must reopen it)."""
    progress_replies: int | None = None
    """v1.2: at least this many progress replies must have been sent to the customer."""
    execution_status_any: list[str] | None = None
    """coverage-v1: the execution record must end in one of these statuses. Replaces the default "done when money
    moves, nothing otherwise" check, so a task can require the honest ``in_doubt`` end (spec 5.3) with or without money
    having moved."""


@dataclass
class EvalTask:
    task_id: str
    dataset: str
    family: str
    mechanisms: list[str]
    business_line: str
    language: str
    disclosure: str
    risk: list[str]
    capabilities: list[str]
    customer_id: str
    user_text: str
    world: dict[str, Any]
    disclosable: dict[str, Any]
    expected: Expected
    reference_extraction: dict[str, Any]
    reference_path: list[dict[str, Any]]
    mechanism_derivation: str | None = None
    spec_refs: list[str] = field(default_factory=list)
    lineage: dict[str, Any] = field(default_factory=lambda: {"parent_task_id": None, "mutation": None})
    scripted_answers: list[str] | None = None
    approval_script: dict[str, Any] = field(default_factory=lambda: {"decision": "approve", "note": None})
    fault_plan: dict[str, int] | None = None
    followups: list[dict[str, Any]] = field(default_factory=list)
    budgets: dict[str, int] | None = None
    reference_reply: str | None = None
    reference_followups: list[str] | None = None
    """v1.2: the author's classification of each customer message the classifier sees while a proposal waits."""
    review: dict[str, Any] = field(default_factory=lambda: {"status": "machine_written", "by": None, "at": None, "notes": ""})
    versions: dict[str, str] = field(default_factory=lambda: {"contract": CONTRACT_VERSION, "dataset": "1.0.0", "fixture": "v1", "rubric": "v1"})
    exposure: dict[str, Any] = field(default_factory=lambda: {"status": "dev", "exposed_at": None, "exposed_by": None})
    notes: str = ""

    # -- serialisation -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # v1.2 fields are left out while unset, so every task written before them keeps its digest.
        for key in _OPTIONAL_TASK_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        for key in _OPTIONAL_EXPECTED_FIELDS:
            if data["expected"].get(key) is None:
                data["expected"].pop(key, None)
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvalTask":
        data = dict(raw)
        data["expected"] = Expected(**data["expected"])
        return cls(**data)

    # -- convenience used by the runner and the artifacts -----------------------

    @property
    def case_id(self) -> str:  # artifacts.capture_trial addresses tasks by ``case_id``
        return self.task_id

    @property
    def category(self) -> str:
        return self.business_line


# -- validation -----------------------------------------------------------------


def _ids_in(text: str) -> set[str]:
    return set(ID_RE.findall(text or ""))


def validate_task(task: EvalTask) -> list[str]:
    """Schema, entity-relationship and spec-consistency checks. Empty list = valid."""
    e: list[str] = []
    t = task
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,120}$", t.task_id):
        e.append("task_id: bad format")
    if t.business_line not in BUSINESS_LINES:
        e.append(f"business_line: {t.business_line!r}")
    if t.language not in LANGUAGES:
        e.append(f"language: {t.language!r}")
    if t.disclosure not in DISCLOSURES:
        e.append(f"disclosure: {t.disclosure!r}")
    if not t.risk or any(r not in RISKS for r in t.risk):
        e.append(f"risk: {t.risk!r}")
    if not t.capabilities or any(c not in CAPABILITIES for c in t.capabilities):
        e.append(f"capabilities: {t.capabilities!r}")
    if not t.mechanisms or any(not MECHANISM_RE.match(m) for m in t.mechanisms):
        e.append(f"mechanisms: {t.mechanisms!r}")
    if any(m.startswith("NEW:") for m in t.mechanisms) and not (t.mechanism_derivation or "").strip():
        e.append("mechanism_derivation: required for a NEW mechanism")
    if t.review.get("status") not in REVIEW_STATUSES:
        e.append(f"review.status: {t.review.get('status')!r}")
    if t.exposure.get("status") not in EXPOSURES:
        e.append(f"exposure.status: {t.exposure.get('status')!r}")
    if t.versions.get("contract") != CONTRACT_VERSION:
        e.append(f"versions.contract: expected {CONTRACT_VERSION}")
    if not t.user_text.strip():
        e.append("user_text: empty")

    # World relationships.
    w = t.world
    customers = {c["id"] for c in w.get("customers", [])}
    invoices = {i["id"]: i for i in w.get("invoices", [])}
    payments = {p["id"]: p for p in w.get("payments", [])}
    refunds = list(w.get("refunds", []))
    if t.customer_id not in customers:
        e.append(f"world: customer {t.customer_id} missing")
    for i in invoices.values():
        if i["customer_id"] not in customers:
            e.append(f"world: invoice {i['id']} has unknown customer")
    for p in payments.values():
        if p["invoice_id"] not in invoices:
            e.append(f"world: payment {p['id']} has unknown invoice")
        elif invoices[p["invoice_id"]]["customer_id"] != p["customer_id"]:
            e.append(f"world: payment {p['id']} customer differs from its invoice")
        if p.get("status") not in ("succeeded", "pending", "failed"):
            e.append(f"world: payment {p['id']} bad status {p.get('status')!r}")
    for r in refunds:
        if r["payment_id"] not in payments:
            e.append(f"world: refund {r['id']} has unknown payment")
        elif payments[r["payment_id"]]["customer_id"] != r["customer_id"]:
            e.append(f"world: refund {r['id']} customer differs from its payment")
        if r.get("status") not in ("pending", "completed", "failed"):
            e.append(f"world: refund {r['id']} bad status {r.get('status')!r}")
    own_invoices = {i for i, row in invoices.items() if row["customer_id"] == t.customer_id}
    own_payments = {p for p, row in payments.items() if row["customer_id"] == t.customer_id}

    # Disclosable: the simulated customer may only reveal ids; whether they are theirs is a design choice.
    for key in ("invoice_id", "payment_id"):
        v = t.disclosable.get(key)
        if v and t.disclosure != "wrong" and v not in (own_invoices | own_payments):
            e.append(f"disclosable.{key}={v} is not this customer's record (set disclosure='wrong' if intended)")
    if t.scripted_answers is not None and not all(isinstance(a, str) for a in t.scripted_answers):
        e.append("scripted_answers: must be strings")

    # Expected.
    x = t.expected
    if not x.outcomes_any:
        e.append("expected.outcomes_any: empty")
    for o in x.outcomes_any:
        if o.get("conclusion") not in CONCLUSIONS:
            e.append(f"expected.outcomes_any: bad conclusion {o.get('conclusion')!r}")
        if o.get("final_state") not in FINAL_STATES:
            e.append(f"expected.outcomes_any: bad final_state {o.get('final_state')!r}")
        if o.get("conclusion") == "escalate" and o.get("final_state") != "escalated":
            e.append("expected.outcomes_any: escalate must end in escalated")
    wants_refund = any(o.get("conclusion") == "refund_proposed" for o in x.outcomes_any)
    if x.refund_delta:
        d = x.refund_delta
        if d.get("payment_id") not in own_payments:
            e.append("expected.refund_delta.payment_id is not this customer's payment")
        if int(d.get("amount_cents", 0)) <= 0:
            e.append("expected.refund_delta.amount_cents must be positive")
        if int(d.get("count", 1)) != 1:
            e.append("expected.refund_delta.count must be 1 (one dispute, one refund)")
        if x.target_payment_id and x.target_payment_id != d.get("payment_id"):
            e.append("expected.target_payment_id differs from refund_delta.payment_id")
        if x.amount_cents is not None and int(x.amount_cents) != int(d.get("amount_cents", -1)):
            e.append("expected.amount_cents differs from refund_delta.amount_cents")
        if not wants_refund:
            e.append("expected.refund_delta given but no outcome is refund_proposed")
        elif d.get("payment_id") in payments:
            # Hand-computed amount must not exceed the payment minus counted refunds.
            p = payments[d["payment_id"]]
            counted = sum(int(r["amount_cents"]) for r in refunds if r["payment_id"] == p["id"] and r["status"] in ("pending", "completed"))
            if int(d["amount_cents"]) > int(p["amount_cents"]) - counted:
                e.append("expected.refund_delta.amount_cents exceeds the payment's remaining balance")
    elif wants_refund and all(o.get("conclusion") == "refund_proposed" for o in x.outcomes_any):
        approved = t.approval_script.get("decision", "approve") == "approve"
        if approved and not any(o.get("final_state") in ("uncertain", "escalated") for o in x.outcomes_any):
            e.append("expected.refund_delta required when every allowed outcome is an approved, executed refund")
    for group in x.required_facts_any:
        for f in group:
            kind = f.get("fact")
            if kind == "tool":  # legacy form
                continue
            if kind not in FACT_KINDS:
                e.append(f"expected.required_facts_any: unknown fact {kind!r}")
                continue
            for arg in FACT_KINDS[kind]:
                if arg not in f:
                    e.append(f"expected.required_facts_any: {kind} needs {arg}")
    for fid in x.forbidden_reads:
        if fid in own_invoices or fid in own_payments:
            e.append(f"expected.forbidden_reads: {fid} belongs to the customer")
    for rule in x.response_rubric:
        if rule.get("polarity") not in ("must", "must_not") or not rule.get("claim") or not rule.get("id"):
            e.append(f"expected.response_rubric: bad rule {rule!r}")

    # Scripts.
    if t.approval_script.get("decision") not in ("approve", "reject"):
        e.append("approval_script.decision")
    if t.fault_plan:
        for k, v in t.fault_plan.items():
            if k not in FAULT_KEYS or int(v) < 0:
                e.append(f"fault_plan: bad entry {k}={v}")
    for kind in t.reference_followups or []:
        if kind not in FOLLOWUP_KINDS:
            e.append(f"reference_followups: {kind!r} is not one of {FOLLOWUP_KINDS}")
    for key in ("max_proposal_version", "min_proposal_version", "progress_replies"):
        value = getattr(t.expected, key)
        if value is not None and (not isinstance(value, int) or value < 1):
            e.append(f"expected.{key}: must be a positive integer")
    if x.execution_status_any is not None:
        if not x.execution_status_any or any(s not in EXECUTION_STATUSES for s in x.execution_status_any):
            e.append(f"expected.execution_status_any: {x.execution_status_any!r}")
        if "in_doubt" in x.execution_status_any and not any(o.get("final_state") in ("uncertain", "escalated") for o in x.outcomes_any):
            e.append("expected.execution_status_any: in_doubt needs an uncertain or escalated outcome")
    for fu in t.followups:
        if fu.get("when") not in FOLLOWUP_STATES or fu.get("command") not in FOLLOWUP_COMMANDS:
            e.append(f"followups: bad entry {fu!r}")
        if fu.get("command") == "message" and not fu.get("text"):
            e.append("followups: message needs text")
        if fu.get("command") == "billing_patch":
            e.extend(_patch_errors(fu, payments, refunds))

    # Reference extraction may only carry what the message says.
    said = _ids_in(t.user_text)
    for key in ("invoice_ids", "payment_ids"):
        for v in t.reference_extraction.get(key, []):
            if v not in said:
                e.append(f"reference_extraction.{key}: {v} is not in user_text")
    # Reference path must be a linear investigation ending in end_investigation.
    if not t.reference_path or t.reference_path[-1].get("tool") != "end_investigation":
        e.append("reference_path: must end with end_investigation")
    for step in t.reference_path:
        if not isinstance(step.get("args"), dict):
            e.append(f"reference_path: step {step!r} needs args")
    return e


def _patch_errors(fu: Mapping[str, Any], payments: Mapping[str, Mapping[str, Any]], refunds: list[Mapping[str, Any]]) -> list[str]:
    e: list[str] = []
    patch = fu.get("patch")
    if fu.get("when") != "awaiting_approval":
        e.append("followups: billing_patch only applies while a proposal waits for approval")
    if not isinstance(patch, dict) or set(patch) != {"refunds"} or not patch["refunds"]:
        return e + ["followups: billing_patch.patch must be {'refunds': [...]} with at least one row"]
    known = {r["id"] for r in refunds}
    for r in patch["refunds"]:
        if r.get("id") in known:
            e.append(f"followups: billing_patch refund {r.get('id')} already exists")
        p = payments.get(r.get("payment_id"))
        if p is None or p["customer_id"] != r.get("customer_id"):
            e.append(f"followups: billing_patch refund {r.get('id')} does not match a payment of its customer")
        if r.get("status") not in ("pending", "completed", "failed") or int(r.get("amount_cents", 0)) <= 0:
            e.append(f"followups: billing_patch refund {r.get('id')} has a bad status or amount")
    return e


def apply_patches(world: Mapping[str, Any], patches: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The starting world as the scorer must see it once third-party patches were applied."""
    out = json.loads(json.dumps(world))
    for patch in patches:
        for table, rows in patch.items():
            out.setdefault(table, []).extend(json.loads(json.dumps(rows)))
    return out


# -- lineage -----------------------------------------------------------------------


def lineage_violations(tasks: Iterable[EvalTask], occupied: Mapping[str, str]) -> list[str]:
    """Family isolation: no sealed family may sit on a mechanism the development side occupies."""
    out: list[str] = []
    for t in tasks:
        for m in t.mechanisms:
            if m in occupied:
                out.append(f"{t.task_id}: mechanism {m} is occupied by {occupied[m]}")
    return out


def occupied_from_matrix(path: Path) -> dict[str, str]:
    """Parse the 'occupied' table of docs/CAPABILITY_MATRIX.md: rows '| D-01 | owner |'."""
    occupied: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([DSPXE]-\d{2})\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            occupied[m.group(1)] = m.group(2)
    return occupied


def family_summary(tasks: Iterable[EvalTask]) -> dict[str, Any]:
    """Counts only. This is all the development side may see of a sealed set."""
    tasks = list(tasks)
    by = lambda key: sorted({str(getattr(t, key)) for t in tasks})  # noqa: E731
    fam: dict[str, int] = {}
    for t in tasks:
        fam[t.family] = fam.get(t.family, 0) + 1
    return {
        "tasks": len(tasks),
        "families": len(fam),
        "variants_per_family": sorted(set(fam.values())),
        "business_lines": {b: sum(1 for t in tasks if t.business_line == b) for b in BUSINESS_LINES},
        "languages": {lang: sum(1 for t in tasks if t.language == lang) for lang in LANGUAGES},
        "risk": {r: sum(1 for t in tasks if r in t.risk) for r in RISKS},
        "review": {s: sum(1 for t in tasks if t.review.get("status") == s) for s in REVIEW_STATUSES},
        "datasets": by("dataset"),
    }


# -- legacy adapter -------------------------------------------------------------


def _legacy_outcomes(gt: Mapping[str, Any]) -> list[dict[str, str]]:
    conclusions = [gt["expected_conclusion"], *(gt.get("expected_conclusion_any") or [])]
    states = [gt.get("expected_final_state", "closed"), *(gt.get("expected_final_state_any") or [])]
    out: list[dict[str, str]] = []
    for c in dict.fromkeys(conclusions):
        for s in dict.fromkeys(states):
            if c == "escalate" and s != "escalated":
                continue
            if c != "escalate" and s == "escalated":
                continue
            out.append({"conclusion": c, "final_state": s})
    return out or [{"conclusion": gt["expected_conclusion"], "final_state": gt.get("expected_final_state", "closed")}]


def from_legacy_case(case: Mapping[str, Any], *, dataset: str, exposure: str, language: str = "en") -> EvalTask:
    """Read-only adapter for the pre-contract ``Case`` records. Nothing is recomputed."""
    gt = case["ground_truth"]
    delta = None
    if gt.get("expected_conclusion") == "refund_proposed" and gt.get("expected_target_payment_id"):
        delta = {"payment_id": gt["expected_target_payment_id"], "amount_cents": int(gt.get("expected_amount_cents") or 0), "count": 1}
    facts = [[{"fact": "tool", **f} for f in group] for group in gt.get("required_facts_any", [])]
    has_ids = bool(_ids_in(case["user_text"]))
    text_l = case["user_text"].lower()
    ext = {
        "invoice_ids": sorted(set(re.findall(r"\bINV-[A-Za-z0-9-]+\b", case["user_text"]))),
        "payment_ids": sorted(set(re.findall(r"\bPAY-[A-Za-z0-9-]+\b", case["user_text"]))),
        "amounts_cents": [int(round(float(m) * 100)) for m in re.findall(r"(\d+(?:\.\d{1,2})?) yuan", case["user_text"])],
        "mentions_duplicate": case["category"] == "duplicate",
        "mentions_refund_status": case["category"] == "refund_status",
        "mentions_subscription_price": case["category"] == "subscription",
        "requests_price_adjustment": case["category"] == "subscription" and any(w in text_l for w in ("refund", "credit", "money back", "pay me", "reverse", "return")) and "do not want" not in text_l,
        "summary": case["user_text"][:200],
    }
    return EvalTask(
        task_id=case["case_id"], dataset=dataset, family=case["family"], mechanisms=["NEW:legacy"],
        mechanism_derivation=f"legacy family {case['family']}", business_line=case["category"], language=language,
        disclosure="full" if has_ids else ("partial" if case.get("disclosable") else "none"),
        risk=["write"] if delta else ["read_only"], capabilities=["C1", "C2", "C4"],
        customer_id=next(c["id"] for c in case["world"]["customers"] if c["id"] == "C-1001") if any(c["id"] == "C-1001" for c in case["world"]["customers"]) else case["world"]["customers"][0]["id"],
        user_text=case["user_text"], world=case["world"], disclosable=case.get("disclosable") or {},
        expected=Expected(
            outcomes_any=_legacy_outcomes(gt), target_payment_id=gt.get("expected_target_payment_id"),
            amount_cents=gt.get("expected_amount_cents"), refund_delta=delta, required_facts_any=facts,
            forbidden_reads=list(gt.get("forbidden_ok_reads", [])),
            must_clarify=True if gt.get("clarification_expected") else None, verdict_reason=gt.get("expected_verdict_reason"),
        ),
        reference_extraction=ext, reference_path=[{"tool": "end_investigation", "args": {"conclusion_type": "insufficient_information", "summary": "legacy task: no reference path recorded", "facts": []}}],
        review={"status": "rule_validated", "by": "legacy-generator", "at": None, "notes": "adapted from pre-contract Case; reference_path is a placeholder"},
        exposure={"status": exposure, "exposed_at": None, "exposed_by": None},
    )


# -- files and freezing ----------------------------------------------------------


def load_tasks(path: Path) -> list[EvalTask]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ValueError(
            "The legacy EvalTask runner requires a JSON list of v1 tasks. "
            "Versioned business-v2 fixture bundles must use their own validator; "
            "fixture feasibility does not make them ready for Agent execution."
        )
    return [EvalTask.from_dict(r) for r in raw]


def write_tasks(path: Path, tasks: Iterable[EvalTask]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([t.to_dict() for t in tasks], indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def freeze(path: Path, *, role: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write ``<path>.manifest.json`` with per-task digests. Refuses to overwrite an existing manifest."""
    path = Path(path)
    manifest_path = path.with_suffix(".manifest.json")
    if manifest_path.exists():
        raise FileExistsError(f"{manifest_path} exists; a frozen set is never re-frozen in place")
    tasks = load_tasks(path)
    errors = {t.task_id: validate_task(t) for t in tasks}
    errors = {k: v for k, v in errors.items() if v}
    if errors:
        raise ValueError(f"cannot freeze: {json.dumps(errors, ensure_ascii=False)[:2000]}")
    manifest = {
        "frozen_at": utc_now(),
        "contract": CONTRACT_VERSION,
        "role": role,
        "task_set_sha256": digest([t.to_dict() for t in tasks]),
        "task_digests": {t.task_id: digest(t.to_dict()) for t in tasks},
        "summary": family_summary(tasks),
        **(dict(extra) if extra else {}),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def verify_frozen(path: Path) -> list[str]:
    path = Path(path)
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    tasks = load_tasks(path)
    problems = []
    if manifest["task_set_sha256"] != digest([t.to_dict() for t in tasks]):
        problems.append("task set digest differs from manifest")
    for t in tasks:
        if manifest["task_digests"].get(t.task_id) != digest(t.to_dict()):
            problems.append(f"{t.task_id}: digest differs")
    return problems
