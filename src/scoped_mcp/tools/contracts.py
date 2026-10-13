from scoped_mcp.models import Contract, ContractStatus
from scoped_mcp.registry import NotFound, tool
from scoped_mcp.tenancy import TenantView


@tool(scopes=["contracts:read"])
def list_contracts(view: TenantView, status: ContractStatus | None = None) -> list[Contract]:
    """List your organisation's contracts, optionally only those with one status (active, expired or draft)."""
    return [c for c in view.contracts() if status is None or c.status == status]


@tool(scopes=["contracts:read"])
def get_contract(view: TenantView, contract_id: str) -> Contract:
    """Fetch one contract by id, for example CTR-2001."""
    contract = view.contract(contract_id)
    if contract is None:
        raise NotFound("contract not found")
    return contract
