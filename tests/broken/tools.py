"""Tools that break the contract on purpose, on their own registry so the real server never sees them.

Each one is a mistake that reads as reasonable in review.
"""

from scoped_mcp.models import Invoice
from scoped_mcp.registry import Denied, NotFound, Registry
from scoped_mcp.store import Store
from scoped_mcp.tenancy import TenantView

broken = Registry()

_EVERYTHING = Store.from_fixtures()


@broken.tool(scopes=["invoices:read"])
def check_invoice(view: TenantView, invoice_id: str) -> Invoice:
    """Says "not yours" for another tenant's id and "not found" for a missing one, so the caller learns which ids exist."""
    if invoice := view.invoice(invoice_id):
        return invoice
    if any(i.id == invoice_id for i in view._store.invoices):
        raise Denied("that invoice belongs to another organisation")
    raise NotFound("invoice not found")


@broken.tool(scopes=["invoices:read"])
def recent_invoices(view: TenantView) -> list[Invoice]:
    """The three newest invoices, sorted from the raw rows, and the tenant filter got lost on the way."""
    return sorted(view._store.invoices, key=lambda i: i.issued, reverse=True)[:3]


@broken.tool(scopes=["invoices:read", "invoices:write"])
def void_invoice(view: TenantView, invoice_id: str) -> str:
    """Writes by id alone, so any tenant can change any tenant's invoice."""
    for row in view._store.invoices:
        if row.id == invoice_id:
            row.status = "paid"
            return "ok"
    return "ok"


@broken.tool(scopes=["invoices:read"])
def paid_revenue(view: TenantView) -> int:
    """Reads a module-level store instead of the view, so it sums every tenant's revenue."""
    return sum(i.amount_cents for i in _EVERYTHING.invoices if i.status == "paid")


@broken.tool(scopes=["invoices:read"])
def invoices_matching(view: TenantView, filters: dict) -> list[Invoice]:
    """Takes a parameter type the noninterference sweep has no probe values for."""
    return [i for i in view.invoices() if all(getattr(i, k) == v for k, v in filters.items())]
