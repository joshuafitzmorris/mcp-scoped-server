"""Start the test issuer and the MCP server, sign three users in with PKCE, and make some calls.

    uv run python -m scoped_mcp.demo           # the walkthrough, then exit
    uv run python -m scoped_mcp.demo --serve   # keep both running for curl or your own client
"""

import json
import sys

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import scoped_mcp.tools  # noqa: F401  registers the tools
from scoped_mcp.auth.client import sign_in
from scoped_mcp.local import CLIENT_ID, REDIRECT_URI, LocalStack, local_stack
from scoped_mcp.registry import SCOPES, registry


async def main(serve: bool) -> None:
    async with local_stack(registry) as stack:
        print(f"issuer      {stack.issuer_url}")
        print(f"mcp server  {stack.mcp_url}\n")
        if serve:
            print("Serving until Ctrl-C.")
            await anyio.sleep_forever()

        async with httpx2.AsyncClient() as http:
            anonymous = await http.post(stack.mcp_url, json={})
            print(f"no token: HTTP {anonymous.status_code}, {anonymous.headers['www-authenticate']}\n")

        await as_user(stack, "ana@quillmoor.example", [
            ("list_invoices", {"status": "open"}),
            ("get_invoice", {"invoice_id": "INV-1002"}),  # Tarnwick's invoice
            ("invoice_totals", {}),
        ])
        await as_user(stack, "ben@quillmoor.example", [
            ("mark_invoice_paid", {"invoice_id": "INV-1005"}),  # ben holds no invoices:write
        ])
        await as_user(stack, "cora@tarnwick.example", [
            ("search_invoices", {"query": "Harbor Lane"}),  # a customer both tenants share
            ("invoice_totals", {}),
        ])


async def as_user(stack: LocalStack, subject: str, calls: list[tuple[str, dict]]) -> None:
    async with httpx2.AsyncClient() as http:
        token = await sign_in(
            http,
            mcp_url=stack.mcp_url,
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            login_hint=subject,
            scopes=sorted(SCOPES),  # ask for everything; the issuer grants what the user holds
        )
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as authed,
        Client(streamable_http_client(stack.mcp_url, http_client=authed)) as client,
    ):
        profile = (await client.call_tool("whoami", {})).structured_content or {}
        tools = [t.name for t in (await client.list_tools()).tools]
        print(f"{subject}  tenant={profile.get('tenant')}  scopes={' '.join(profile.get('scopes', []))}")
        print(f"  sees {len(tools)} tools: {', '.join(tools)}")
        for name, args in calls:
            result = await client.call_tool(name, args)
            print(f"  {name}({_args(args)}) -> {_summary(result)}")
    print()


def _args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def _summary(result) -> str:
    if result.is_error:
        return f"error: {result.content[0].text}"
    data = (result.structured_content or {}).get("result", result.structured_content)
    if isinstance(data, list):
        return ", ".join(f"{r['id']} {r['customer']} {r['status']}" for r in data) or "(none)"
    return json.dumps(data)


if __name__ == "__main__":
    anyio.run(main, "--serve" in sys.argv)
