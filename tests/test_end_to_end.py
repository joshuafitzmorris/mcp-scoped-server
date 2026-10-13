"""The real thing on loopback ports: the issuer, the MCP server, and the SDK's own client over streamable HTTP."""

from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from scoped_mcp.auth.client import sign_in
from scoped_mcp.local import CLIENT_ID, OTHER_RESOURCE, REDIRECT_URI, LocalStack, local_stack
from scoped_mcp.registry import SCOPES, registry

pytestmark = pytest.mark.anyio


@pytest.fixture
async def stack():
    async with local_stack(registry) as s:
        yield s


async def token_for(stack: LocalStack, subject: str, resource: str | None = None) -> str:
    async with httpx2.AsyncClient() as http:
        return await sign_in(
            http,
            mcp_url=stack.mcp_url,
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            login_hint=subject,
            scopes=sorted(SCOPES),
            resource=resource,
        )


@asynccontextmanager
async def connected(stack: LocalStack, token: str):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
        Client(streamable_http_client(stack.mcp_url, http_client=http)) as client,
    ):
        yield client


async def test_no_token_gets_a_401_that_points_at_the_issuer(stack) -> None:
    async with httpx2.AsyncClient() as http:
        response = await http.post(stack.mcp_url, json={})
        assert response.status_code == 401
        metadata_url = response.headers["www-authenticate"].split('resource_metadata="')[1].rstrip('"')
        metadata = (await http.get(metadata_url)).json()
    assert metadata["resource"] == stack.mcp_url
    assert metadata["authorization_servers"] == [stack.issuer_url]


async def test_a_signed_in_user_sees_only_their_tenant(stack) -> None:
    async with connected(stack, await token_for(stack, "ana@quillmoor.example")) as client:
        assert len((await client.list_tools()).tools) == 8
        listed = await client.call_tool("list_invoices", {})
        assert {i["tenant"] for i in listed.structured_content["result"]} == {"quillmoor"}
        theirs = await client.call_tool("get_invoice", {"invoice_id": "INV-1002"})
        assert theirs.is_error and theirs.content[0].text.endswith("invoice not found")


async def test_a_user_without_the_write_scope_cannot_write(stack) -> None:
    async with connected(stack, await token_for(stack, "ben@quillmoor.example")) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "mark_invoice_paid" not in names
        result = await client.call_tool("mark_invoice_paid", {"invoice_id": "INV-1005"})
    assert result.is_error and result.content[0].text == "mark_invoice_paid needs scope invoices:write"
    assert next(i for i in stack.store.invoices if i.id == "INV-1005").status == "open"


async def test_a_write_lands_in_the_callers_tenant_only(stack) -> None:
    async with connected(stack, await token_for(stack, "cora@tarnwick.example")) as client:
        assert not (await client.call_tool("mark_invoice_paid", {"invoice_id": "INV-1004"})).is_error
        assert (await client.call_tool("mark_invoice_paid", {"invoice_id": "INV-1001"})).is_error
    statuses = {i.id: i.status for i in stack.store.invoices}
    assert statuses["INV-1004"] == "paid" and statuses["INV-1001"] == "open"


async def test_a_token_minted_for_another_server_is_refused(stack) -> None:
    token = await token_for(stack, "ana@quillmoor.example", resource=OTHER_RESOURCE)
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        assert (await http.post(stack.mcp_url, json={})).status_code == 401
