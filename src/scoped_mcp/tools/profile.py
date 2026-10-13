from scoped_mcp.models import Profile
from scoped_mcp.registry import tool
from scoped_mcp.tenancy import TenantView


@tool(scopes=["profile:read"])
def whoami(view: TenantView) -> Profile:
    """Who you are signed in as, which organisation you belong to, and what you are allowed to do."""
    return Profile(tenant=view.tenant, tenant_name=view.tenant_name, subject=view.subject, scopes=sorted(view.scopes))
