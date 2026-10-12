"""Customer consent must match the displayed batch, not merely contain yes."""
import pytest

from billingops.investigation.b1 import confirmation_answer, confirmation_scope


def scope(amount=4900):
    return confirmation_scope('INV-2026-0142', [
        {'payment_id': p, 'amount_cents': amount, 'currency': 'CNY'}
        for p in ('PAY-88213', 'PAY-88214')
    ])


@pytest.mark.parametrize('answer', [
    '确认', '同意。', 'Yes', 'Yes, please.', 'I agree.', 'Go ahead.',
    '确认，两笔多扣的钱全部退回。', '确认全部退款。',
    '两笔49元都退，合计98元。', '确认两笔各49元，合计98元。',
    '确认，每笔49元都退。',
    'Yes, please refund both extra payments on INV-2026-0142, totaling 98 yuan.',
    'Yes, refund both, 49 yuan each, 98 yuan in total.',
    '是的，把INV-2026-0142多扣的两笔共98元都退回。',
    '确认退回PAY-88213和PAY-88214，合计98元。',
])
def test_clear_acknowledgement_or_consistent_full_scope_is_accepted(answer):
    assert confirmation_answer(answer, scope()) == 'confirm'


@pytest.mark.parametrize('answer', [
    '确认，先退49元。', '请你确认一下金额是多少？', '同意先退一笔。',
    'Yes, refund 49 yuan.', 'All right, do nothing yet.',
    '确认退10元', '确认只退一笔', 'Yes, but only one', 'not approved',
    '改成另一张账单', '同意除了第一笔', '确认金额改为1元',
    '确认每笔98元', '确认两笔共49元', '确认3笔共98元',
    'Yes, refund one payment for 98 yuan.',
    '确认退PAY-88213，合计98元',
    '确认退PAY-88213和PAY-OTHER，合计98元',
    '确认退INV-OTHER多扣两笔98元',
    '确认，98', '确认九十八元全部退回', 'Yes, refund ninety-eight yuan.',
    '确认每笔49元，共97元', 'Yes, refund both for 98 dollars.',
    '确认退-98元', '确认至少98元', 'Yes, refund both if it is refundable.',
    '请确认两笔98元都能退吗', '确认两笔98元？',
    '确认，但是暂缓执行', '同意，不用真的退款', '同意，等明天再退',
    '确认，但接下来先等我的银行回复', 'Yes, subject to my bank approval.',
    '同意，这是演示而已', 'Yes, this is a hypothetical request.',
])
def test_question_hold_partial_or_changed_scope_does_not_create_consent(answer):
    assert confirmation_answer(answer, scope()) != 'confirm'


@pytest.mark.parametrize('amount', [1, 10000])
def test_scope_amount_is_dynamic_not_hardcoded_to_98(amount):
    total = amount * 2 / 100
    assert confirmation_answer(f'是的，把INV-2026-0142多扣的两笔共{total:.2f}元都退回。', scope(amount)) == 'confirm'
    assert confirmation_answer('确认退98元全部两笔。', scope(amount)) != 'confirm'


def test_without_scope_only_short_unqualified_acknowledgement_can_be_recognized():
    assert confirmation_answer('Yes') == 'confirm'
    assert confirmation_answer('确认') == 'confirm'
    assert confirmation_answer('Yes, refund 49 yuan.') == 'unclear'
    assert confirmation_answer('确认金额改为1元') != 'confirm'
