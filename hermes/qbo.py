"""QuickBooks Online API client for HermesOrch.

Scope: just enough to find-or-create a customer and create an invoice against
a sandbox company. OAuth2 refresh is handled in-memory; refresh token is the
only credential the caller needs to hold long-term.

The QBO API uses a REST surface rooted at /v3/company/{realmId}/... with
JSON payloads. Full docs: https://developer.intuit.com/app/developer/qbapi/docs/
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from hermes.config import settings

log = logging.getLogger(__name__)


_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
_APP_URL = {
    "sandbox": "https://sandbox.qbo.intuit.com",
    "production": "https://qbo.intuit.com",
}


@dataclass
class InvoiceResult:
    ok: bool
    invoice_id: str | None = None
    invoice_url: str | None = None
    doc_number: str | None = None
    customer_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class QBOClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access_token: str | None = settings.qbo_access_token or None
        # QBO access tokens live 3600s. Be conservative — refresh 60s early.
        self._access_expires_at: float = time.time() + 3500 if self._access_token else 0.0
        self._refresh_token: str = settings.qbo_refresh_token
        self._client_id = settings.qbo_client_id
        self._client_secret = settings.qbo_client_secret
        self._realm_id = settings.qbo_realm_id
        self._env = settings.qbo_environment or "sandbox"

    # --- credential state ---

    @property
    def configured(self) -> bool:
        return bool(
            self._client_id
            and self._client_secret
            and self._refresh_token
            and self._realm_id
        )

    def _ensure_token(self) -> None:
        with self._lock:
            if self._access_token and time.time() < self._access_expires_at:
                return
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        if not (self._client_id and self._client_secret and self._refresh_token):
            raise RuntimeError("QBO not configured (missing client_id/secret/refresh_token)")
        auth = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        resp = httpx.post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"QBO token refresh failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._access_expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
        # QBO rotates refresh tokens occasionally; update if present.
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        log.info("QBO token refreshed; next refresh in %.0fs", self._access_expires_at - time.time())

    # --- low-level request ---

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self._ensure_token()
        url = f"{_API_BASE[self._env]}{path}"
        headers = kwargs.pop("headers", {}) or {}
        headers.update(
            {
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        return httpx.request(method, url, headers=headers, timeout=30, **kwargs)

    # --- domain operations ---

    def find_or_create_customer(
        self,
        display_name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        # 1) search by DisplayName (safe quote for SQL-ish query language)
        safe = display_name.replace("'", "\\'")
        q = f"select * from Customer where DisplayName = '{safe}'"
        r = self._request(
            "GET",
            f"/v3/company/{self._realm_id}/query",
            params={"query": q, "minorversion": "73"},
        )
        if r.status_code == 200:
            rows = r.json().get("QueryResponse", {}).get("Customer", [])
            if rows:
                return rows[0]

        # 2) create
        body: dict[str, Any] = {"DisplayName": display_name}
        if email:
            body["PrimaryEmailAddr"] = {"Address": email}
        if phone:
            body["PrimaryPhone"] = {"FreeFormNumber": phone}
        r = self._request(
            "POST",
            f"/v3/company/{self._realm_id}/customer",
            params={"minorversion": "73"},
            json=body,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"QBO customer create failed: {r.status_code} {r.text}")
        return r.json()["Customer"]

    def _default_service_item_id(self) -> str:
        """Pick a services-ish item for line items. Sandbox companies usually
        ship with 'Services' (id=1). Falls back to the first item found."""
        r = self._request(
            "GET",
            f"/v3/company/{self._realm_id}/query",
            params={
                "query": "select * from Item where Type = 'Service' maxresults 1",
                "minorversion": "73",
            },
        )
        if r.status_code == 200:
            rows = r.json().get("QueryResponse", {}).get("Item", [])
            if rows:
                return str(rows[0]["Id"])
        return "1"

    def create_invoice(
        self,
        *,
        customer_display_name: str,
        amount: float,
        description: str,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        customer_id: str | None = None,
    ) -> InvoiceResult:
        if not self.configured:
            return InvoiceResult(ok=False, error="QBO not configured")

        try:
            # Dedup guard: if caller already knows the QBO customer id (set
            # during intake via find_qbo_customer_by_contact), skip the
            # find-or-create round trip entirely.
            if not customer_id:
                customer = self.find_or_create_customer(
                    customer_display_name, email=customer_email, phone=customer_phone
                )
                customer_id = str(customer["Id"])
            service_item_id = self._default_service_item_id()

            body = {
                "CustomerRef": {"value": customer_id},
                "Line": [
                    {
                        "Amount": round(float(amount), 2),
                        "DetailType": "SalesItemLineDetail",
                        "Description": description,
                        "SalesItemLineDetail": {
                            "ItemRef": {"value": service_item_id},
                        },
                    }
                ],
            }
            r = self._request(
                "POST",
                f"/v3/company/{self._realm_id}/invoice",
                params={"minorversion": "73"},
                json=body,
            )
            if r.status_code not in (200, 201):
                return InvoiceResult(
                    ok=False,
                    error=f"invoice create failed: {r.status_code} {r.text[:500]}",
                )
            inv = r.json()["Invoice"]
            inv_id = str(inv["Id"])
            doc_num = str(inv.get("DocNumber") or inv_id)
            # QBO invoice view URL is convention-based; the sandbox UI uses this.
            url = f"{_APP_URL[self._env]}/app/invoice?txnId={inv_id}"
            return InvoiceResult(
                ok=True,
                invoice_id=inv_id,
                invoice_url=url,
                doc_number=doc_num,
                customer_id=customer_id,
                raw=inv,
            )
        except Exception as e:
            log.exception("QBO create_invoice error")
            return InvoiceResult(ok=False, error=str(e))


qbo = QBOClient()
