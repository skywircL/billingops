"""B2 protocol checks. Invalid model output is not missing business evidence.

The small validator implements the keywords used by the public tool schemas;
it rejects malformed arguments before reads, pauses, or conclusions occur.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import re
from typing import Any

from . import b1


EXTRACT_SCHEMA = deepcopy(b1.EXTRACT_SCHEMA)
EXTRACT_SCHEMA['input_schema']['properties'].update({
    'price_adjustment_kind': {
        'type': 'string',
        'enum': ['none', 'price_increase_credit', 'unused_period_refund', 'cancellation_refund'],
        'description': 'Only an explicit request to reverse an increase, refund unused service, or refund cancellation. An unexplained extra charge is none until investigated.',
    },
    'price_adjustment_basis': {
        'type': ['string', 'null'],
        'description': 'Exact customer-message excerpt that identifies the increase, unused service, or cancellation. Null when requests_price_adjustment is false. A request to refund extra/duplicate money is not evidence of a price adjustment.',
    },
})
EXTRACT_SCHEMA['input_schema']['required'].extend(['price_adjustment_kind', 'price_adjustment_basis'])


def schema_error(value: Any, schema: dict[str, Any], path: str = '$') -> str | None:
    """Return a bounded explanation, without copying untrusted raw arguments."""
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    matches = {
        'null': value is None, 'object': isinstance(value, dict),
        'array': isinstance(value, list), 'string': isinstance(value, str),
        'integer': type(value) is int, 'number': type(value) in (int, float),
        'boolean': type(value) is bool,
    }
    if types and not any(matches.get(t, False) for t in types):
        return f'{path}: expected {" or ".join(types)}'
    if 'enum' in schema and value not in schema['enum']:
        return f'{path}: value is not an allowed enum'
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        for key in schema.get('required', []):
            if key not in value:
                return f'{path}.{key}: required field missing'
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            return f'{path}: unexpected argument'
        for key, item in value.items():
            if key in properties and (error := schema_error(item, properties[key], f'{path}.{key}')):
                return error
    if isinstance(value, list):
        if len(value) > schema.get('maxItems', float('inf')):
            return f'{path}: too many items'
        for i, item in enumerate(value):
            if error := schema_error(item, schema.get('items', {}), f'{path}[{i}]'):
                return error
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', float('inf')):
            return f'{path}: text length outside allowed range'
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            return f'{path}: identifier does not match the allowed format'
    return None


def extraction_error(arguments: dict[str, Any], customer_text: str) -> str | None:
    if error := schema_error(arguments, EXTRACT_SCHEMA['input_schema']):
        return error
    flag = arguments['requests_price_adjustment']
    kind, basis = arguments['price_adjustment_kind'], arguments['price_adjustment_basis']
    if not flag:
        return None if kind == 'none' and basis is None else 'price_adjustment: false requires kind=none and basis=null'
    if kind == 'none' or not basis or basis not in customer_text:
        return 'price_adjustment: true requires a specific kind and verbatim customer evidence'
    # These are conservative evidence checks, never a source of refund authority.
    # Unrecognized phrasing is sent back for correction, not silently reclassified.
    anchors = {
        'price_increase_credit': r'涨价|提价|调价|涨幅|价格上涨|原价|旧价|(?:price|rate|fee).{0,30}(?:increase|hike|rise)|(?:increase|hike|rise).{0,30}(?:price|rate|fee)|(?:old|previous|original)\s+price',
        'unused_period_refund': r'未用|没用|未使用|剩余(?:时间|期限|月份)|unused|remaining\s+(?:period|term|months?|time)',
        'cancellation_refund': r'取消|退订|cancel',
    }
    if not re.search(anchors[kind], basis, re.I):
        return 'price_adjustment: excerpt does not identify the selected adjustment; unexplained extra charges require investigation, not an assumed price dispute'
    return None


def failure_conclusion(text: str, reason: str, at: str) -> dict[str, Any]:
    reply = (
        '自动调查因系统处理异常中断，需要人工核查后继续。本次调查没有发起新的退款；这不表示您没有可退金额。'
        if re.search(r'[\u4e00-\u9fff]', text) else
        'The automated investigation stopped because of a processing problem and needs manual review. '
        'This investigation did not issue a new refund; this does not mean that no refund is due.'
    )
    return {'type': 'escalate', 'text': reply, 'target_invoice_id': None, 'target_payment_id': None,
            'verdict': None, 'facts': [], 'concluded_at': at, 'runtime_failure_reason': reason}


def question_amount_error(question: str, invoices: dict[str, dict[str, Any]]) -> str | None:
    """Catch an explicit invoice amount stated in yuan using the cents integer.

    Deliberately narrow: only a locally named, observed CNY invoice and an
    explicit amount-due assertion; customer-quoted claims are not checked as
    system facts. This is not a general natural-language fact checker.
    """
    for invoice_id, invoice in invoices.items():
        cents = invoice.get('amount_cents')
        if invoice.get('currency') != 'CNY' or type(cents) is not int or cents <= 0:
            continue
        pattern = re.escape(invoice_id) + r'(?P<between>(?:(?!INV-|PAY-|[。；;\n]).){0,80}?)(?:应付(?:金额)?|amount\s+due)\s*[:：为是]?\s*(?P<amount>\d+(?:\.\d+)?)\s*(?:元|yuan|CNY)'
        for match in re.finditer(pattern, question, re.I):
            before = question[max(0, match.start() - 15):match.start()] + match.group('between')
            if re.search(r'您(?:说|提到|提供)|你(?:说|提到)|客户(?:说|提到)|you\s+(?:said|mentioned)|customer\s+(?:said|claimed)|[“"‘]', before, re.I):
                continue
            if Decimal(match.group('amount')) == cents:
                return f'currency_unit_mismatch: {invoice_id} amount_cents={cents} is {cents / 100:.2f} yuan, not {cents} yuan; correct the question before asking the customer'
    return None
