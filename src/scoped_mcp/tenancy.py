"""TenantView: the only data handle a tool receives.

Every method filters on the caller's tenant before it returns anything, and
returns copies, so a tool can't change a stored row by mutating what it got.
A record that exists under another tenant looks exactly like one that doesn't
exist at all.
"""

from typing import TYPE_CHECKING

from scoped_mcp.models import Contract, Invoice, InvoiceStatus

if TYPE_CHECKING:
    from scoped_mcp.store import Store


class TenantView:
    __slots__ = ("_store", "_tenant", "_subject", "_scopes")

    def __init__(self, store: "Store", *, tenant: str, subject: str, scopes: frozenset[str]):
        self._store = store
        self._tenant = tenant
        self._subject = subject
        self._scopes = scopes

    @property
    def tenant(self) -> str:
        return self._tenant

    @property
    def tenant_name(self) -> str:
        return self._store.tenant_names[self._tenant]

    @property
    def subject(self) -> str:
        return self._subject

    @property
    def scopes(self) -> frozenset[str]:
        return self._scopes

    def invoices(self) -> list[Invoice]:
        return [i.model_copy() for i in self._store.invoices if i.tenant == self._tenant]

    def invoice(self, invoice_id: str) -> Invoice | None:
        return next((i for i in self.invoices() if i.id == invoice_id), None)

    def contracts(self) -> list[Contract]:
        return [c.model_copy() for c in self._store.contracts if c.tenant == self._tenant]

    def contract(self, contract_id: str) -> Contract | None:
        return next((c for c in self.contracts() if c.id == contract_id), None)

    def set_invoice_status(self, invoice_id: str, status: InvoiceStatus) -> Invoice | None:
        for row in self._store.invoices:
            if row.tenant == self._tenant and row.id == invoice_id:
                row.status = status
                return row.model_copy()
        return None
