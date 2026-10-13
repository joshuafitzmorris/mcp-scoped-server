"""The runtime half: what happens to a single call, in process and through the MCP server."""

import pytest
from mcp import Client

from scoped_mcp.registry import (
    CrossTenantResult,
    Denied,
    NotFound,
    Principal,
    ToolFailure,
    invoke,
    registry,
)
from scoped_mcp.server import build_server
from scoped_mcp.store import Store
from scoped_mcp.tenancy import TenantView
from tests.broken.tools import broken

ANA = Principal("ana@quillmoor.example", "quillmoor", frozenset({"profile:read", "invoices:read", "invoices:write", "contracts:read"}))
BEN = Principal("ben@quillmoor.example", "quillmoor", frozenset({"profile:read", "invoices:read"}))


def call(name: str, principal: Principal | None, store: Store | None = None, **args):
    return invoke(registry, name, principal, args, store or Store.from_fixtures())


def test_an_unknown_tool_is_denied() -> None:
    with pytest.raises(Denied, match="not a declared tool"):
        call("delete_everything", ANA)


def test_a_call_without_a_principal_is_denied() -> None:
    with pytest.raises(Denied, match="no authenticated caller"):
        call("list_invoices", None)


def test_a_missing_scope_is_denied_and_named() -> None:
    store = Store.from_fixtures()
    with pytest.raises(Denied, match="needs scope invoices:write"):
        call("mark_invoice_paid", BEN, store, invoice_id="INV-1005")
    assert next(i for i in store.invoices if i.id == "INV-1005").status == "open"


def test_results_hold_only_the_callers_tenant() -> None:
    assert {i.tenant for i in call("list_invoices", ANA)} == {"quillmoor"}
    assert [i.id for i in call("search_invoices", ANA, query="harbor lane")] == ["INV-1001", "INV-1005"]


def test_another_tenants_id_looks_exactly_like_a_missing_one() -> None:
    with pytest.raises(NotFound) as theirs:
        call("get_invoice", ANA, invoice_id="INV-1002")
    with pytest.raises(NotFound) as missing:
        call("get_invoice", ANA, invoice_id="INV-0000")
    assert str(theirs.value) == str(missing.value)


def test_a_write_cannot_reach_another_tenants_row() -> None:
    store = Store.from_fixtures()
    with pytest.raises(NotFound):
        call("mark_invoice_paid", ANA, store, invoice_id="INV-1004")
    assert next(i for i in store.invoices if i.id == "INV-1004").status == "open"


def test_bad_arguments_are_a_tool_failure() -> None:
    with pytest.raises(ToolFailure, match="bad arguments"):
        call("get_invoice", ANA, id="INV-1001")


def test_a_result_holding_another_tenants_record_is_withheld() -> None:
    with pytest.raises(CrossTenantResult):
        invoke(broken, "recent_invoices", ANA, {}, Store.from_fixtures())


def test_the_view_hands_out_copies() -> None:
    store = Store.from_fixtures()
    view = TenantView(store, tenant="quillmoor", subject="x", scopes=frozenset())
    view.invoices()[0].status = "paid"
    assert store.invoices[0].status == "open"


def test_each_caller_sees_only_the_tools_their_scopes_allow() -> None:
    assert len(registry.visible_to(ANA)) == 8
    assert {t.name for t in registry.visible_to(BEN)} == {
        "whoami",
        "list_invoices",
        "get_invoice",
        "search_invoices",
        "invoice_totals",
    }
    assert registry.visible_to(None) == []


@pytest.mark.anyio
async def test_a_server_with_no_auth_lists_nothing_and_refuses_every_call() -> None:
    server = build_server(registry=registry, store=Store.from_fixtures(), auth=None)
    async with Client(server) as client:
        assert (await client.list_tools()).tools == []
        for t in registry:
            result = await client.call_tool(t.name, {})
            assert result.is_error and result.content[0].text == "no authenticated caller"
