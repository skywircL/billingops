"""Parallel evaluation environments must retain their own real HTTP state."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import threading

import pytest

from billingops.billing_client import BillingClient
from billingops.testing import BillingServer, World


@pytest.mark.parametrize("worker_count", [8, 16])
def test_parallel_billing_instances_isolate_worlds_and_refund_keys(tmp_path: Path, worker_count):
    barrier = threading.Barrier(worker_count, timeout=30)

    def worker(index: int):
        server = BillingServer(tmp_path / str(index) / "billing.sqlite3")
        client = None
        amount = 1000 + index
        try:
            # Every worker reserves its port before any starts listening.
            barrier.wait()
            server.start()
            client = BillingClient(server.url, admin_token=server.admin_token)
            world = (World().customer("C-SHARED", name=f"worker-{index}")
                     .invoice("INV-SHARED", "C-SHARED", amount, "2026-10-01", "2026-10-31")
                     .payment("PAY-RETAINED", "C-SHARED", "INV-SHARED", amount, "2026-10-01")
                     .payment("PAY-SHARED", "C-SHARED", "INV-SHARED", amount, "2026-10-02")
                     .build())
            client.load_world(world)
            barrier.wait()  # A shared service would now contain another world.
            assert client.get_invoice("C-SHARED", "INV-SHARED")["amount_cents"] == amount
            assert client.dump_world()["customers"][0]["name"] == f"worker-{index}"
            response = client.post_refund({
                "idempotency_key": "same-action-key-all-workers",
                "dispute_key": "same-dispute-key-all-workers",
                "customer_id": "C-SHARED", "payment_id": "PAY-SHARED",
                "amount_cents": amount, "currency": "CNY",
            })
            assert response.status == 201
            barrier.wait()  # Identical keys must not collide across databases.
            receipt = client.refund_by_key("same-action-key-all-workers")
            assert receipt["status"] == "succeeded"
            assert receipt["amount_cents"] == amount
            refunds = client.payment_refunds("C-SHARED", "PAY-SHARED")
            assert len(refunds) == 1 and refunds[0]["amount_cents"] == amount
            return server.port, server.db_path
        finally:
            if client:
                client.close()
            server.stop()
            assert not server._thread.is_alive()
            assert server._socket.fileno() == -1

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(worker, range(worker_count)))
    assert len({port for port, _ in results}) == worker_count
    assert len({db_path for _, db_path in results}) == worker_count


def test_port_stays_reserved_until_stop_even_before_start(tmp_path: Path):
    server = BillingServer(tmp_path / "billing.sqlite3")
    try:
        with socket.socket() as competing:
            with pytest.raises(OSError):
                competing.bind(("127.0.0.1", server.port))
    finally:
        server.stop()
    with socket.socket() as replacement:
        replacement.bind(("127.0.0.1", server.port))


def test_failed_startup_cannot_accept_unrelated_health_and_releases_port(tmp_path: Path, monkeypatch):
    import billingops.testing as testing

    health_requests = []
    monkeypatch.setattr(testing.uvicorn.Server, "run", lambda self, sockets=None: None)
    monkeypatch.setattr(testing.httpx, "get", lambda *args, **kwargs: health_requests.append(args))
    server = BillingServer(tmp_path / "billing.sqlite3")
    with pytest.raises(RuntimeError, match="exited during startup"):
        server.start()
    assert health_requests == []
    assert not server._thread.is_alive()
    assert server._socket.fileno() == -1
    with socket.socket() as replacement:
        replacement.bind(("127.0.0.1", server.port))
