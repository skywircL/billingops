"""HTTP client for the Billing service.

Reads carry a timeout and no retries of their own: the caller counts reads
against a budget and a retried read would be a hidden second read. The write
has **no retries at all**, at any layer. The application sends one POST per
action and treats anything other than a parsed response as an unknown outcome
to be verified by key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import BILLING_READ_TIMEOUT_S, BILLING_WRITE_TIMEOUT_S


class BillingUnavailable(RuntimeError):
    """A read failed for a reason that is not 'not found'."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class WriteResponse:
    """Whatever came back from the single refund POST."""

    status: int
    body: dict[str, Any] | None


class BillingClient:
    def __init__(self, base_url: str, *, admin_token: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token
        # No transport-level retries: httpx does none by default, and this
        # client never adds any.
        self._client = httpx.Client(base_url=self.base_url, transport=transport, timeout=BILLING_READ_TIMEOUT_S)

    def close(self) -> None:
        self._client.close()

    # -- reads -------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> Any:
        try:
            response = self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as exc:
            raise BillingUnavailable(f"billing read failed: {exc}") from exc
        if response.status_code == 404:
            raise NotFound(response.json().get("detail", "not found"))
        if response.status_code >= 400:
            raise BillingUnavailable(f"billing read returned {response.status_code}", status=response.status_code)
        return response.json()

    def list_invoices(self, customer_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/customers/{customer_id}/invoices"))

    def get_invoice(self, customer_id: str, invoice_id: str) -> dict[str, Any]:
        return dict(self._get(f"/customers/{customer_id}/invoices/{invoice_id}"))

    def period_invoices(self, customer_id: str, invoice_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/customers/{customer_id}/invoices/{invoice_id}/period"))

    def invoice_payments(self, customer_id: str, invoice_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/customers/{customer_id}/invoices/{invoice_id}/payments"))

    def get_payment(self, customer_id: str, payment_id: str) -> dict[str, Any]:
        return dict(self._get(f"/customers/{customer_id}/payments/{payment_id}"))

    def payment_refunds(self, customer_id: str, payment_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/customers/{customer_id}/payments/{payment_id}/refunds"))

    def customer_refunds(self, customer_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/customers/{customer_id}/refunds"))

    def subscription_history(self, customer_id: str) -> dict[str, Any]:
        return dict(self._get(f"/customers/{customer_id}/subscription-history"))

    def get_policy(self, name: str, version: int | None = None) -> dict[str, Any]:
        return dict(self._get(f"/policies/{name}", version=version))

    # -- the write and its verification --------------------------------------

    def post_refund(self, payload: dict[str, Any]) -> WriteResponse:
        """Send the refund exactly once. Raises on anything short of a response."""
        response = self._client.post("/refunds", json=payload, timeout=BILLING_WRITE_TIMEOUT_S)
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        return WriteResponse(status=response.status_code, body=body)

    def refund_by_key(self, key: str) -> dict[str, Any] | None:
        """One verification read. None means 404, which proves nothing about later commits."""
        try:
            response = self._client.get(f"/refunds/by-key/{key}")
        except httpx.HTTPError as exc:
            raise BillingUnavailable(f"verification failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise BillingUnavailable(f"verification returned {response.status_code}", status=response.status_code)
        return dict(response.json())

    # -- admin (tests, demos, experiments) --------------------------------------

    def _admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Token": self.admin_token or ""}

    def load_world(self, world: dict[str, Any]) -> dict[str, Any]:
        response = self._client.put("/_admin/world", json=world, headers=self._admin_headers(), timeout=30)
        response.raise_for_status()
        return dict(response.json())

    def dump_world(self) -> dict[str, Any]:
        response = self._client.get("/_admin/world", headers=self._admin_headers(), timeout=30)
        response.raise_for_status()
        return dict(response.json())

    def arm_faults(self, **spec: int) -> dict[str, Any]:
        response = self._client.post("/_faults", json=spec, headers=self._admin_headers())
        response.raise_for_status()
        return dict(response.json())

    def fault_snapshot(self) -> dict[str, Any]:
        """The faults still armed (remaining uses per key)."""
        response = self._client.get("/health", headers=self._admin_headers())
        response.raise_for_status()
        return dict(response.json().get("faults") or {})

    def clear_faults(self) -> None:
        self._client.delete("/_faults", headers=self._admin_headers()).raise_for_status()
