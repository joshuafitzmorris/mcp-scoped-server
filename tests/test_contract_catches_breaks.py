"""The contract's own tests: each check, pointed at a deliberately broken tool, must fail it.

A check that has never been seen failing proves nothing. The `demo/broken-tool`
branch shows the same failures through the real test suite.
"""

import inspect

import pytest
from mcp import Client

from scoped_mcp import contract
from scoped_mcp.registry import Registry, ScopedTool, registry
from scoped_mcp.server import UndeclaredTool, build_server, checked_app
from scoped_mcp.store import Store
from scoped_mcp.tenancy import TenantView
from tests.broken.tools import broken


@pytest.mark.anyio
async def test_a_tool_added_straight_to_the_server_is_reported_refused_and_denied() -> None:
    server = build_server(registry=registry, store=Store.from_fixtures(), auth=None)

    @server.tool()
    def health() -> str:
        """Liveness check."""
        return "ok"

    assert [str(v) for v in await contract.server_declarations(server, registry)] == [
        "health: undeclared: on the MCP server without a scope declaration from the registry"
    ]
    with pytest.raises(UndeclaredTool, match="health"):
        await checked_app(server, registry)
    async with Client(server) as client:
        result = await client.call_tool("health", {})
    assert result.is_error and result.content[0].text == "health is not a declared tool"


def test_a_tool_without_a_usable_declaration_cannot_be_registered() -> None:
    r = Registry()
    with pytest.raises(TypeError):
        r.tool()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="at least one scope"):
        r.tool(scopes=[])
    with pytest.raises(ValueError, match="unknown scope"):
        r.tool(scopes=["invoices:delete"])
    with pytest.raises(TypeError, match="TenantView"):
        r.tool(scopes=["invoices:read"])(lambda store, invoice_id: None)


def test_a_declaration_emptied_after_registration_is_reported() -> None:
    r = Registry()
    r.add(ScopedTool("sneaky", lambda view: None, frozenset(), "", inspect.Signature([])))
    assert [str(v) for v in contract.scope_declarations(r)] == ["sneaky: undeclared: declares no scopes"]


def test_back_doors_are_reported_with_their_lines() -> None:
    found = {v.detail.split(": ", 1)[1] for v in contract.back_doors(broken)}
    assert found == {
        "imports Store from scoped_mcp.store",
        "touches private attribute ._store",
        "uses getattr()",
    }


def test_noninterference_reports_each_kind_of_leak() -> None:
    by_tool = {v.tool: v.rule for v in contract.noninterference(broken)}
    assert by_tool == {
        "check_invoice": "reads another tenant",  # "not yours" vs "not found" gives away which ids exist
        "recent_invoices": "reads another tenant",
        "void_invoice": "writes another tenant",
        "invoices_matching": "unprobeable",
        # paid_revenue is missing on purpose: it ignores the store it is handed, so comparing
        # stores can't see it. back_doors catches its import; nothing catches it at run time.
    }


def test_a_regression_in_the_tenant_view_shows_up_on_every_tool_that_reads_through_it(monkeypatch) -> None:
    monkeypatch.setattr(TenantView, "invoices", lambda self: [i.model_copy() for i in self._store.invoices])
    leaking = {v.tool for v in contract.noninterference(registry)}
    assert leaking == {"list_invoices", "get_invoice", "search_invoices", "invoice_totals"}
