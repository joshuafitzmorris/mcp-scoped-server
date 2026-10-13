"""The tool contract. CI fails the build when any of these fail.

What each one guarantees, and what it doesn't, is in the README under "What CI guarantees".
test_contract_catches_breaks.py shows each one failing on a deliberately broken tool.
"""

import pytest

from scoped_mcp import contract
from scoped_mcp.registry import registry
from scoped_mcp.server import build_server
from scoped_mcp.store import Store


def explain(violations: list[contract.Violation]) -> str:
    return "\n".join(str(v) for v in violations)


@pytest.mark.anyio
async def test_every_tool_on_the_server_declares_its_scopes() -> None:
    server = build_server(registry=registry, store=Store.from_fixtures(), auth=None)
    violations = await contract.server_declarations(server, registry) + contract.scope_declarations(registry)
    assert not violations, explain(violations)


def test_no_tool_module_reaches_past_the_tenant_view() -> None:
    violations = contract.back_doors(registry)
    assert not violations, explain(violations)


def test_no_tool_reads_or_writes_another_tenants_data() -> None:
    violations = contract.noninterference(registry)
    assert not violations, explain(violations)


def test_the_contract_covers_every_tool() -> None:
    # guards: the checks passing vacuously because the tools never got registered
    assert {t.name for t in registry} >= {"whoami", "list_invoices", "get_invoice", "mark_invoice_paid", "get_contract"}
