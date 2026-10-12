"""Post-fix branch validation, frozen before live runs; not an independent benchmark."""
from pathlib import Path
from copy import deepcopy

from ..testing import World, duplicate_world, ambiguous_world
from .challenges import INV, EXTRA, fact, truth, build_challenges
from .data import Case, CUSTOMER
from .artifacts import write_json, digest, utc_now, source_manifest


def build_cases():
    out = []
    safe = truth('insufficient_information', amount=None, total=0, target=None, facts=[[]],
                 expected_conclusion_any=['escalate'], expected_final_state_any=['escalated'])
    def add(name, text, world, gold, disclosable=None, category='duplicate'):
        out.append(Case('v3-'+name, 'v3_'+name, category, text, world, disclosable or {}, deepcopy(gold)))
    w=duplicate_world()
    for n in range(4):
        noise=World().invoice(f'INV-EDGE-{n}',CUSTOMER,7300,'2026-03-01','2026-03-31').payment(f'PAY-EDGE-{n}',CUSTOMER,f'INV-EDGE-{n}',7300,'2026-03-02').build()
        for table in ('invoices','payments'): w[table].extend(noise[table])
    add('unique-budget-edge','My statements show a duplicate charge. I lost the transaction reference; please investigate the extra payment.',w,truth())
    w=deepcopy(w)
    w['invoices'].extend(World().invoice('INV-UNREAD',CUSTOMER,4900,'2026-01-01','2026-01-31').build()['invoices'])
    # Seven invoices require 1 listing + 7 payment reads + 1 refund read: cannot safely propose within eight.
    add('budget-incomplete','I was charged twice. I have no reference. Do not guess if the investigation limit prevents checking it.',w,safe)
    w=duplicate_world(); w['payments']=[p for p in w['payments'] if p['id']!=EXTRA]
    add('zero-duplicates-unknown','I suspect a duplicate charge but have no invoice number. Can you investigate?',w,safe)
    w=duplicate_world(); w['payments'].extend(World().payment('PAY-THIRD',CUSTOMER,INV,4900,'2026-02-03T09:00:00').build()['payments'])
    add('three-payments-ambiguous',f'I was charged more than once on {INV}. I cannot distinguish the transactions. Do not pick one for me.',w,safe)
    add('three-payments-selected',f'There were duplicate payments on {INV}. Please handle extra payment PAY-THIRD specifically.',w,
        truth(target='PAY-THIRD',facts=[[fact('list_payments',invoice_id=INV),fact('list_refunds',payment_id='PAY-THIRD')]]))
    add('original-payment-selected','I suspect a duplicate charge. Please refund PAY-88212; this is the transaction I mean.',duplicate_world(),safe)
    add('two-duplicates-selected',f'Both months were charged twice, but today please handle only invoice {INV}.',ambiguous_world(),truth())
    add('corrected-identifier','Invoice INV-TYPO-01 was charged twice; my confirmed second payment reference is PAY-88213.',duplicate_world(),truth())
    price=build_challenges()[6].world
    add('credit-paraphrase','The subscription increase took an extra 10 yuan. Please reverse that increase and return the difference to the card.',price,
        truth('escalate',amount=None,total=0,target=None,state='escalated',facts=[[fact('get_subscription_history'),fact('get_policy',name='pricing')]]),category='subscription')
    add('credit-negated','My subscription changed from 39 to 49 yuan. I do not want a refund or credit; just explain the price change.',price,
        truth('explain_subscription',amount=None,total=0,target=None,facts=[[fact('get_subscription_history'),fact('get_policy',name='pricing')]]),category='subscription')
    add('unverifiable-target','I was charged twice on INV-OTHER-ACCOUNT. I have no corrected reference. Check it without substituting another charge.',duplicate_world(),safe)
    w=duplicate_world(prior_refund_cents=2100); w['refunds'].extend(World().refund('RF-PENDING-FULL',EXTRA,CUSTOMER,2800,'2026-02-07',status='pending').build()['refunds'])
    add('pending-covers-remainder',f'I was charged twice on {INV}. Can you refund the duplicate again? Some money is still pending.',w,
        truth('no_refund_due',amount=None,total=4900,target=EXTRA,facts=[[fact('list_payments',invoice_id=INV),fact('list_refunds',payment_id=EXTRA)]]))
    return out


if __name__ == '__main__':
    cases=[c.to_dict() for c in build_cases()]
    path=Path('data/v3_validation.json')
    write_json(path,cases)
    write_json(path.with_suffix('.manifest.json'),{'frozen_at':utc_now(),'task_set_sha256':digest(cases),
        'role':'Post-fix prospective branch validation; scenarios informed by known mechanisms, not a random held-out or official tau score.',
        'protocol':'One A and one B run per case, no behavior or scoring changes after inspecting live results. New failures go to backlog. Prose reviewed separately.',
        'limits':'12 targeted cases, single trial, no population or reliability estimate. Data reviewed by coding agent, independent human review pending.',
        'source':source_manifest()})
