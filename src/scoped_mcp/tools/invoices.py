from scoped_mcp.models import Invoice, InvoiceStatus, Totals
from scoped_mcp.registry import NotFound, ToolFailure, tool
from scoped_mcp.tenancy import TenantView


@tool(scopes=["invoices:read"])
def list_invoices(view: TenantView, status: InvoiceStatus | None = None) -> list[Invoice]:
    """List your organisation's invoices, optionally only those with one status (open, paid or overdue)."""
    return [i for i in view.invoices() if status is None or i.status == status]


@tool(scopes=["invoices:read"])
def get_invoice(view: TenantView, invoice_id: str) -> Invoice:
    """Fetch one invoice by id, for example INV-1001."""
    invoice = view.invoice(invoice_id)
    if invoice is None:
        raise NotFound("invoice not found")
    return invoice


@tool(scopes=["invoices:read"])
def search_invoices(view: TenantView, query: str) -> list[Invoice]:
    """Find invoices whose customer name or memo contains the query text. Case-insensitive."""
    needle = query.strip().lower()
    if not needle:
        raise ToolFailure("query is empty")
    return [i for i in view.invoices() if needle in i.customer.lower() or needle in i.memo.lower()]


@tool(scopes=["invoices:read"])
def invoice_totals(view: TenantView, status: InvoiceStatus | None = None) -> Totals:
    """Count and sum your organisation's invoices, in cents, optionally only those with one status."""
    matching = [i for i in view.invoices() if status is None or i.status == status]
    return Totals(tenant=view.tenant, status=status, count=len(matching), total_cents=sum(i.amount_cents for i in matching))


@tool(scopes=["invoices:read", "invoices:write"])
def mark_invoice_paid(view: TenantView, invoice_id: str) -> Invoice:
    """Record that an invoice has been paid in full. Returns the updated invoice."""
    invoice = view.set_invoice_status(invoice_id, "paid")
    if invoice is None:
        raise NotFound("invoice not found")
    return invoice
