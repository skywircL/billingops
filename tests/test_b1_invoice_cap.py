"""The invoice invariant must survive different payment IDs and concurrent writes."""
import threading


def request(payment_id, key):
    return {'idempotency_key': key, 'dispute_key': f'C-1001:duplicate_charge:{payment_id}',
            'customer_id': 'C-1001', 'payment_id': payment_id, 'amount_cents': 4900, 'currency': 'CNY'}


def test_concurrent_distinct_payments_cannot_refund_below_invoice_due(billing):
    responses = []
    barrier = threading.Barrier(2)
    def worker(pid):
        barrier.wait()
        responses.append(billing.post_refund(request(pid, f'race:{pid}')))
    threads = [threading.Thread(target=worker, args=(pid,)) for pid in ('PAY-88212', 'PAY-88213')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.status for r in responses) == [201, 422]
    rejection = next(r for r in responses if r.status == 422)
    assert rejection.body['rejection_code'] == 'exceeds_invoice_overcharge'
    assert sum(r['amount_cents'] for r in billing.customer_refunds('C-1001')) == 4900
    replay = billing.post_refund(request(rejection.body['payment_id'], rejection.body['idempotency_key']))
    assert replay.status == 422 and replay.body == rejection.body
