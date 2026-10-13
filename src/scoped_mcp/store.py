"""The raw store: every tenant's rows in one place.

Only tenancy.py and the contract checks import this module. Tools get a
TenantView, and the CI contract fails any tool module that imports this one.
In production this is a database, and the same rule belongs there too, as
row-level security keyed on the tenant.
"""

import json
from dataclasses import dataclass
from importlib import resources

from scoped_mcp.models import Contract, Invoice, User


@dataclass
class Store:
    tenant_names: dict[str, str]
    users: list[User]
    invoices: list[Invoice]
    contracts: list[Contract]

    @classmethod
    def from_fixtures(cls) -> "Store":
        raw = json.loads(resources.files("scoped_mcp").joinpath("fixtures.json").read_text())
        return cls(
            tenant_names=raw["tenants"],
            users=[User.model_validate(u) for u in raw["users"]],
            invoices=[Invoice.model_validate(i) for i in raw["invoices"]],
            contracts=[Contract.model_validate(c) for c in raw["contracts"]],
        )

    def tenants(self) -> list[str]:
        return sorted(self.tenant_names)

    def user(self, subject: str) -> User | None:
        return next((u for u in self.users if u.subject == subject), None)

    def only(self, tenant: str) -> "Store":
        """A copy holding one tenant's rows. The noninterference check compares against it."""
        return Store(
            tenant_names={tenant: self.tenant_names[tenant]},
            users=[u for u in self.users if u.tenant == tenant],
            invoices=[i.model_copy() for i in self.invoices if i.tenant == tenant],
            contracts=[c.model_copy() for c in self.contracts if c.tenant == tenant],
        )

    def snapshot_except(self, tenant: str) -> list[dict]:
        """Every other tenant's rows, for checking that a call as `tenant` changed none of them."""
        rows = [*self.invoices, *self.contracts]
        return [r.model_dump() for r in rows if r.tenant != tenant]
