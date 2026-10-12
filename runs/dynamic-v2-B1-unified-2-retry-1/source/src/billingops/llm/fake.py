"""Deterministic stand-ins for the model.

:class:`ScriptedModel` plays a competent investigator: it reads the transcript
and returns the next sensible single tool call. It exists so the protocol, the
budgets, recovery and the execution path can be tested without a key, and so
the harness has something to run when no key is present. Nothing it does is
evidence about the real model.

:class:`ChaosModel` wraps another model and misbehaves on demand: two tool
calls in one turn, prose instead of a tool call, an unknown tool. The protocol
layer has to catch each of those, and a test that cannot produce them is not
testing the protocol.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..investigation import rules
from ..investigation.prompts import EVIDENCE_MARKER, EXTRACTED_MARKER
from ..tools import CLARIFY_TOOL, END_TOOL, EXTRACT_TOOL, FOLLOWUP_TOOL
from .base import Message, ModelConfig, ModelResponse, ToolCall, Usage

# Boundaries are ASCII alphanumerics, not ``\b``: Chinese customers often write an id right next to a CJK character
# ("账单INV-2026-0142扣了两次"), which ``\b`` does not treat as a boundary (coverage-v1 acceptance, 2026-09-27).
INVOICE_RE = re.compile(r"(?<![A-Za-z0-9])INV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
PAYMENT_RE = re.compile(r"(?<![A-Za-z0-9])PAY-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")

DUP_WORDS = ("twice", "double", "duplicate", "two charges", "two identical", "two times", "charged again", "second one", "same day", "重复", "两次", "扣了两")
STATUS_WORDS = ("refund status", "where is my refund", "my refund", "refunded yet", "refund for payment", "refund was issued", "been completed", "check my refund", "still waiting", "refund arrive",
                "退款进度", "退款到", "的退款", "退款申请", "申请的退款", "退款什么时候", "能到账", "退款已经完成", "退款是否")
SUB_WORDS = ("price", "plan", "subscription", "upgrade", "downgrade", "prorat", "涨价", "套餐", "订阅", "increase", "went up")
# v1.2 offline heuristics. The live extractor decides semantically; these only have to be
# right on the plain test phrasings and fail safe elsewhere.
POLICY_WORDS = ("refund policy", "policy on refunds", "your refund rules", "退款政策", "退款规则", "退款条件")
# Hypothetical only when the conditional clause itself describes the duplicate charge:
# "refund it if you can identify it" is a real claim, "if I were charged twice, would you refund?" is not.
HYPOTHETICAL_DUP_RE = re.compile(
    r"(?:如果|假如|要是|万一|\bif\b|\bin case\b)[^。.!?？！]{0,40}?(?:重复|两次|扣了两|twice|double|duplicate|charged again)",
    re.IGNORECASE,
)
PRICE_QUESTION_WORDS = ("why", "为什么", "涨", "price", "价格", "多少钱", "increase", "went up", "charged", "扣")
OUT_OF_SCOPE_RE = re.compile(
    r"change (?:my|the) (?:card|payment method|email)|update (?:my|the) (?:card|payment method|email|billing email)|new card|"
    r"cancel (?:my|the) subscription|change my plan|switch my plan|downgrade (?:me|my plan)|invoice title|how (?:do i|to) (?:use|set up|configure)|"
    r"换卡|换一张卡|扣款的卡|绑定的卡|支付方式|改邮箱|修改邮箱|换邮箱|改套餐|换套餐|降级到|套餐.{0,10}降到|取消订阅|发票抬头|开发票|开票|怎么用|怎么设置|如何设置|怎么配置",
    re.IGNORECASE,
)
WITHDRAW_WORDS = ("不用退", "不退了", "不要退", "撤销", "取消退款", "不需要退款", "don't need the refund", "do not need the refund", "cancel the refund", "withdraw", "no longer want the refund", "don't want the refund")
PROGRESS_WORDS = ("进度", "到哪", "多久", "什么时候", "审批了吗", "status", "when will", "how long", "any update", "progress")


def _extract_fields(text: str) -> dict[str, Any]:
    amounts: list[int] = []
    for m in re.finditer(r"(?<![\w.])(\d{1,6})(?:\.(\d{2}))?\s*(?:yuan|元|RMB|CNY|¥|￥)|(?:¥|￥|RMB\s?|CNY\s?)(\d{1,6})(?:\.(\d{2}))?", text):
        whole = m.group(1) or m.group(3)
        frac = m.group(2) or m.group(4) or "00"
        if whole:
            amounts.append(int(whole) * 100 + int(frac))
    lowered = text.lower()
    hypothetical = bool(HYPOTHETICAL_DUP_RE.search(text))
    asks_policy = any(w in lowered for w in POLICY_WORDS) or hypothetical
    actual = HYPOTHETICAL_DUP_RE.sub(" ", text).lower()  # what is left once the hypothetical clause is removed
    price_adjustment = rules.price_adjustment_request(text)
    out_of_scope = bool(OUT_OF_SCOPE_RE.search(text)) and not price_adjustment
    subscription_price = any(w in lowered for w in SUB_WORDS)
    if out_of_scope and not any(w in lowered for w in PRICE_QUESTION_WORDS):
        subscription_price = False  # "change my plan" is not a question about the price
    return {
        "invoice_ids": sorted(set(INVOICE_RE.findall(text))),
        "payment_ids": sorted(set(PAYMENT_RE.findall(text))),
        "amounts_cents": amounts,
        # "if I were charged twice, would you refund it?" asks about the policy; it does not claim a charge.
        "mentions_duplicate": any(w in actual for w in DUP_WORDS),
        "mentions_refund_status": any(w in lowered for w in STATUS_WORDS) and not asks_policy,
        "mentions_subscription_price": subscription_price,
        "requests_price_adjustment": price_adjustment,
        "requests_other_account": rules.other_account_request(text),
        "asks_refund_policy": asks_policy,
        "requests_out_of_scope": out_of_scope,
        "summary": text[:200],
    }


def classify_followup_text(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in WITHDRAW_WORDS):
        return "withdraw"
    if any(w in lowered for w in PROGRESS_WORDS):
        return "progress"
    return "unclear"


@dataclass
class Transcript:
    """What the scripted investigator can see, parsed once per call."""

    text: str = ""
    extracted: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[ToolCall, dict[str, Any] | None]] = field(default_factory=list)
    """Every assistant tool call paired with its result payload (None while unanswered)."""

    def results(self, tool: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [(c.arguments, r) for c, r in self.calls if c.name == tool and r is not None]

    def called(self, tool: str, **args: Any) -> bool:
        return any(c.name == tool and all(c.arguments.get(k) == v for k, v in args.items()) for c, _ in self.calls)

    def result_for(self, tool: str, **args: Any) -> dict[str, Any] | None:
        """The latest result for that call. A retried read must be seen by its retry, not by its first failure."""
        for c, r in reversed(self.calls):
            if c.name == tool and all(c.arguments.get(k) == v for k, v in args.items()) and r is not None:
                return r
        return None

    def budget_exhausted(self) -> bool:
        return any(r and "read_budget_exhausted" in str(r.get("error", "")) for _, r in self.calls)

    def answers(self) -> list[str]:
        out: list[str] = []
        for _, r in self.results(CLARIFY_TOOL):
            data = r.get("data") or {}
            if isinstance(data, dict) and data.get("answer"):
                out.append(str(data["answer"]))
        return out


def parse_transcript(messages: Sequence[Message]) -> Transcript:
    t = Transcript()
    by_id: dict[str, int] = {}
    for msg in messages:
        if msg.role == "user" and EXTRACTED_MARKER in msg.content and not t.text:
            head, _, tail = msg.content.partition(EXTRACTED_MARKER)
            t.text = head.replace("Customer message:\n", "", 1).strip()
            try:
                t.extracted = json.loads(tail.strip())
            except json.JSONDecodeError:
                t.extracted = {}
        elif msg.role == "assistant":
            for call in msg.tool_calls:
                by_id[call.id] = len(t.calls)
                t.calls.append((call, None))
        elif msg.role == "tool" and msg.tool_call_id in by_id:
            try:
                payload = json.loads(msg.content)
            except json.JSONDecodeError:
                payload = {"ok": False, "error": msg.content}
            idx = by_id[msg.tool_call_id]
            t.calls[idx] = (t.calls[idx][0], payload if isinstance(payload, dict) else {"ok": True, "data": payload})
    return t


def _ok(r: dict[str, Any] | None) -> Any:
    return r.get("data") if r and r.get("ok") else None


class ScriptedModel:
    """See the module docstring. Stateless: everything comes from the transcript."""

    def __init__(self, *, ask_when_missing: bool = True) -> None:
        self.ask_when_missing = ask_when_missing
        self.calls = 0

    # -- protocol --------------------------------------------------------------

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> ModelResponse:
        self.calls += 1
        if config.tag == "extract":
            text = next((m.content for m in messages if m.role == "user"), "")
            return self._tool(EXTRACT_TOOL, _extract_fields(text), messages)
        if config.tag == "conclude":
            return self._text(self._conclude(messages))
        if config.tag == "followup":
            user = next((m.content for m in messages if m.role == "user"), "")
            text = user.split("Customer message:\n", 1)[-1]
            return self._tool(FOLLOWUP_TOOL, {"kind": classify_followup_text(text), "reason": "keyword"}, messages)
        return self._investigate(parse_transcript(messages), messages)

    def _tool(self, name: str, args: dict[str, Any], messages: Sequence[Message]) -> ModelResponse:
        n = sum(1 for m in messages if m.role == "assistant") + 1
        return ModelResponse(
            message=Message(role="assistant", tool_calls=[ToolCall(id=f"fake-{n}", name=name, arguments=args)]),
            usage=Usage(input_tokens=200 + 50 * len(messages), output_tokens=40),
            stop_reason="tool_use",
            model="fake",
        )

    @staticmethod
    def _text(text: str) -> ModelResponse:
        return ModelResponse(
            message=Message(role="assistant", content=text),
            usage=Usage(input_tokens=400, output_tokens=len(text) // 4 + 1),
            stop_reason="end_turn",
            model="fake",
        )

    # -- the investigator ----------------------------------------------------------

    def _investigate(self, t: Transcript, messages: Sequence[Message]) -> ModelResponse:
        step = rules.next_step(t, ask_when_missing=self.ask_when_missing, batch=False)
        if isinstance(step, rules.Read):
            return self._tool(step.tool, step.args, messages)
        if isinstance(step, rules.Ask):
            return self._tool(CLARIFY_TOOL, {"question": step.question}, messages)
        return self._tool(
            END_TOOL,
            {
                "conclusion_type": step.conclusion_type,
                "target_invoice_id": step.target_invoice_id,
                "target_payment_id": step.target_payment_id,
                "summary": step.summary,
                "facts": rules.facts_from(t),
            },
            messages,
        )

    # -- conclusion text --------------------------------------------------------------

    @staticmethod
    def _conclude(messages: Sequence[Message]) -> str:
        user = next((m.content for m in messages if m.role == "user"), "")
        _, _, tail = user.partition(EVIDENCE_MARKER)
        try:
            bundle = json.loads(tail.strip())
        except json.JSONDecodeError:
            return "We reviewed your account. A specialist will follow up."
        verdict = bundle.get("verdict", {})
        kind = bundle.get("conclusion_type")
        if kind == "refund_proposed" and verdict and verdict.get("eligible"):
            yuan = verdict["amount_cents"] / 100
            return (
                f"We found that invoice {verdict['invoice_id']} was paid more than once. Payment {verdict['payment_id']} "
                f"is the extra charge, and {yuan:.2f} yuan remains refundable. We have raised a refund proposal for that "
                "amount; it will be issued once an operator approves it."
            )
        if kind == "no_refund_due":
            reason = (verdict or {}).get("reason", "the records do not show a refundable extra payment")
            return f"We checked the payments on your invoice. No refund is due right now: {reason.replace('_', ' ')}."
        if kind == "no_duplicate_found":
            return "We checked the invoice and its billing period. Each charge belongs to a separate invoice and only one payment was taken per invoice, so nothing was charged twice."
        if kind == "explain_refund_status":
            refunds = bundle.get("refunds", [])
            if not refunds:
                return "We found no refund recorded for this payment. If you were expecting one, please share the confirmation you received."
            r = refunds[-1]
            return f"Your refund {r['id']} of {r['amount_cents'] / 100:.2f} yuan is {r['status']}" + (f" and was completed on {r['completed_at'][:10]}." if r.get("completed_at") else ". It is still being processed.")
        if kind == "explain_subscription":
            return "Your invoice reflects the plan change recorded on your subscription and the pricing policy in force at that time. The amount charged matches those records."
        if kind == "escalate":
            return "This request needs a specialist's decision, so we have passed it to an operator who will get back to you."
        if kind == "explain_refund_policy":
            return ("Our refund policy: a duplicate charge is refunded for the extra payment once an operator approves it, and price "
                    "disputes are reviewed by an operator. We cannot promise an arrival date.")
        if kind == "explain_out_of_scope":
            return ("This desk handles duplicate charges, refund status, subscription price questions and refund policy questions. "
                    "Please use the billing settings of your account or contact product support for this request.")
        return "We could not identify the charge from the information available. Please reply with the invoice or payment id and we will look again."


@dataclass
class ChaosModel:
    """Misbehaves on chosen investigation calls, delegates otherwise."""

    inner: Any
    double_call_on: set[int] = field(default_factory=set)
    prose_on: set[int] = field(default_factory=set)
    unknown_tool_on: set[int] = field(default_factory=set)
    investigate_calls: int = 0

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> ModelResponse:
        if config.tag != "investigate":
            return self.inner.generate(messages, tool_schemas, config)
        self.investigate_calls += 1
        n = self.investigate_calls
        if n in self.double_call_on:
            return ModelResponse(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id=f"chaos-{n}a", name="list_invoices", arguments={}),
                        ToolCall(id=f"chaos-{n}b", name="list_customer_refunds", arguments={}),
                    ],
                ),
                usage=Usage(input_tokens=100, output_tokens=20),
                stop_reason="tool_use",
                model="chaos",
            )
        if n in self.prose_on:
            return ModelResponse(message=Message(role="assistant", content="Let me think about this..."), usage=Usage(100, 10), stop_reason="end_turn", model="chaos")
        if n in self.unknown_tool_on:
            return ModelResponse(
                message=Message(role="assistant", tool_calls=[ToolCall(id=f"chaos-{n}", name="issue_refund", arguments={"amount_cents": 4900})]),
                usage=Usage(100, 10),
                stop_reason="tool_use",
                model="chaos",
            )
        return self.inner.generate(messages, tool_schemas, config)
