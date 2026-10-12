"""Conservative customer payment scope, derived from the original utterances.

This is a small supported command grammar, not a general language parser. A
restriction we cannot bind to payment IDs remains unresolved. Model labels and
ordinary acknowledgements cannot turn that restriction into invoice-wide consent.
"""
from __future__ import annotations

import re
from typing import Any

_PID = r"PAY-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
_PAYMENTS = re.compile(rf"(?<![A-Za-z0-9]){_PID}(?![A-Za-z0-9])")
_INVOICES = re.compile(r"(?<![A-Za-z0-9])INV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
_RESTRICT = re.compile(
    r"(?:只|最多|就).{0,8}(?:退|处理|办理)|仅|其中.{0,5}(?:一|1)笔|(?:一|1)笔.{0,6}(?:就行|即可)|"
    r"不要|别退|不退|不动|不用|除了|除外|剩下.{0,5}(?:别|不要)|"
    r"\b(?:only|just|except|excluding|instead|not|never)\b|don't|leave.{0,25}(?:alone|untouched)", re.I)
_ONE = re.compile(
    r"(?:只|仅|先|最多|就).{0,12}(?:一|1)笔|其中.{0,5}(?:一|1)笔|(?:一|1)笔.{0,6}(?:就行|即可)|"
    r"\b(?:only|just|at most)\s+(?:(?:refund|return|process|handle)\s+)?(?:one|1)\b|"
    r"\b(?:refund|return|process|handle)\s+(?:only\s+)?(?:one|1)\s+(?:of|payment|charge|transaction)", re.I)
_NEGATIVE = re.compile(r"不要|别|不退|不动|不用|不得|\b(?:not|never|except|excluding)\b|don't|leave.{0,25}(?:alone|untouched)", re.I)
_OP = re.compile(r"退回|退款|退|处理|办理|\b(?:refund|return|handle|process)\b", re.I)
_CHANGE = re.compile(r"改为|改成|更正为|重新选择|改变主意|\b(?:instead|change|changed my mind)\b", re.I)
_ACK = re.compile(r"确认|同意|\b(?:yes|confirm|confirmed|agree)\b", re.I)
_TARGETS = re.compile(
    rf"^(?:\s|的|把|将|这笔|付款|交易|only\s+|just\s+|the\s+|payment\s+|transaction\s+)*"
    rf"(?P<ids>{_PID}(?:\s*(?:和|与|及|、|,|&|\band\b)\s*{_PID})*)(?P<tail>.*)$", re.I)
_TAIL = re.compile(
    r"^(?:\s|[。.!！]|这|那|该|的|笔|一|二|两|三|\d|个|项|重复|多扣|付款|款项|款|交易|都|退回|退款|"
    r"\b(?:payments?|transactions?|charges?|refunds?|only|please|both|these|those|duplicate|extra)\b)*$", re.I)
_BARE = re.compile(rf"^\s*(?:选|选择|就|是|choose\s+)?\s*{_PID}\s*(?:这笔|那笔|please)?[。.!！]?\s*$", re.I)
_OBJECT_FIRST = re.compile(r"^(?:请|麻烦)?(?:把|将)(?P<operand>.+?)(?:都)?(?:退回|退款|退|处理|办理)\s*$")


def _selection(text: str) -> tuple[list[str], list[str], bool]:
    """Return direct positive targets, explicit negatives, and parse uncertainty."""
    selected: set[str] = set()
    excluded: set[str] = set()
    uncertain = False
    # A negative second clause must not become part of the first verb's object.
    # Only a bare ID list can continue a directly preceding positive directive.
    clauses = re.split(r"[。；;!?！？,，]|但是|不过|但|\bbut\b|\bhowever\b|\band\s+(?=(?:please\s+)?(?:refund|don't|do not))", text, flags=re.I)
    continuation = False
    for clause in clauses:
        ids = set(_PAYMENTS.findall(clause))
        if not ids:
            continuation = False
            continue
        if _NEGATIVE.search(clause):
            # Multiple verbs without a clause boundary are not safely parsed.
            if len(_OP.findall(clause)) > 1:
                uncertain = True
            excluded.update(ids)
            continuation = False
            continue
        operation = _OP.search(clause)
        object_first = _OBJECT_FIRST.fullmatch(clause.strip())
        if continuation and _BARE.fullmatch(clause):
            selected.update(ids)
            continue
        continuation = False
        if operation is None and object_first is None:
            continue
        operand = _INVOICES.sub("", object_first['operand'] if object_first else clause[operation.end():])
        match = _TARGETS.fullmatch(operand)
        if match and _TAIL.fullmatch(match['tail']):
            selected.update(_PAYMENTS.findall(match['ids']))
            continuation = True
        elif _PAYMENTS.search(operand):
            uncertain = True
    return sorted(selected), sorted(excluded), uncertain


def resolve_customer_authorization(utterances: list[dict[str, Any]], extracted_role: str = 'unspecified') -> dict[str, Any]:
    """Fold customer restrictions in order; a bare yes never resets the scope.

    Direct target changes can replace a selection before execution. Expanding a
    previously restricted set/count requires an explicit change directive, not
    a confirmation of a larger scope proposed by the assistant.
    """
    current: dict[str, Any] = {
        'scope_mode': 'invoice_all', 'resolution_status': 'resolved',
        'requested_payment_ids': [], 'explicitly_excluded_payment_ids': [],
        'background_only_payment_ids': [], 'max_payment_count': None,
        'retained_payment_ids': [],
        'source_utterance': None, 'source_command_ids': [], 'reason': 'customer_scope_resolved',
    }
    for index, row in enumerate(utterances):
        text = str(row.get('text', ''))
        mentioned = sorted(set(_PAYMENTS.findall(text)))
        restricted = bool(_RESTRICT.search(text))
        one = bool(_ONE.search(text))
        selected, excluded, uncertain = _selection(text)
        existing = current['scope_mode'] != 'invoice_all'
        # A single ID answering an outstanding selection question is evidence;
        # a list copied into an ordinary confirmation is not a scope expansion.
        if current['scope_mode'] == 'unresolved' and _BARE.fullmatch(text):
            selected = mentioned
        relevant = restricted or one or excluded or uncertain or bool(selected)
        if index == 0 and extracted_role == 'exclusive':
            relevant = True
        if not relevant:
            continue
        limit = 1 if one else current['max_payment_count']
        previous = set(current['requested_payment_ids'] or current['retained_payment_ids'])
        expanded = bool(selected and existing and (
            (limit is not None and len(selected) > limit)
            or (previous and not set(selected) <= previous)
        ))
        explicit_change = bool(_CHANGE.search(text)) and not _ACK.search(text)
        if explicit_change and not one:
            limit = len(selected) if selected else limit
        if existing and not explicit_change:
            excluded = sorted(set(excluded) | set(current['explicitly_excluded_payment_ids']))
        unresolved = (uncertain or not selected or bool(set(selected) & set(excluded))
                      or (limit is not None and len(selected) > limit) or (expanded and not explicit_change))
        # A confirmation without a new selection cannot erase the old constraint.
        if existing and not selected and not excluded and not one and not uncertain:
            continue
        current = {
            'scope_mode': 'unresolved' if unresolved else 'selected',
            'resolution_status': 'needs_clarification' if unresolved else 'resolved',
            'requested_payment_ids': [] if unresolved else selected,
            'explicitly_excluded_payment_ids': excluded,
            'background_only_payment_ids': sorted(set(mentioned) - set(selected) - set(excluded)),
            'max_payment_count': limit,
            'retained_payment_ids': sorted(previous) if unresolved else selected,
            'source_utterance': dict(row),
            'source_command_ids': current['source_command_ids'] + ([str(row['command_id'])] if row.get('command_id') else []),
            'reason': 'customer_payment_scope_unresolved' if unresolved else 'customer_scope_resolved',
        }
    return current


def authorizes_items(scope: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    """Check the immutable candidate items against the customer-derived scope."""
    if scope['resolution_status'] != 'resolved':
        return False
    targets = {item['payment_id'] for item in items}
    if targets & set(scope['explicitly_excluded_payment_ids']):
        return False
    limit = scope['max_payment_count']
    if limit is not None and len(targets) > limit:
        return False
    return scope['scope_mode'] == 'invoice_all' or targets == set(scope['requested_payment_ids'])
