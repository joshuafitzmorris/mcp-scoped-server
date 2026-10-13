"""The test issuer and the MCP server on two loopback ports, for the demo and the end-to-end test."""

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp

from scoped_mcp.auth.issuer import RegisteredClient, TestIssuer
from scoped_mcp.auth.verifier import JwtVerifier, remote_jwks
from scoped_mcp.registry import Registry
from scoped_mcp.server import ServerAuth, build_server, checked_app
from scoped_mcp.store import Store

CLIENT_ID = "demo-client"
REDIRECT_URI = "http://127.0.0.1:8976/callback"  # never served: the client reads the code off the redirect
OTHER_RESOURCE = "https://other-mcp.example/mcp"  # a second server the issuer mints tokens for


@dataclass
class LocalStack:
    issuer_url: str
    mcp_url: str
    issuer: TestIssuer
    mcp: MCPServer
    store: Store


@asynccontextmanager
async def local_stack(registry: Registry, store: Store | None = None) -> AsyncIterator[LocalStack]:
    store = store or Store.from_fixtures()
    issuer_sock, issuer_url = _loopback()
    mcp_sock, mcp_base = _loopback()
    mcp_url = f"{mcp_base}/mcp"
    issuer = TestIssuer(
        url=issuer_url,
        users=store.users,
        clients=[RegisteredClient(CLIENT_ID, (REDIRECT_URI,))],
        resources=[mcp_url, OTHER_RESOURCE],
    )
    verifier = JwtVerifier(issuer=issuer_url, audience=mcp_url, jwks=remote_jwks(f"{issuer_url}/jwks.json"))
    mcp = build_server(registry=registry, store=store, auth=ServerAuth(issuer_url, mcp_url, verifier))
    async with _serving(issuer.app(), issuer_sock), _serving(await checked_app(mcp, registry), mcp_sock):
        yield LocalStack(issuer_url, mcp_url, issuer, mcp, store)


def _loopback() -> tuple[socket.socket, str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock, f"http://127.0.0.1:{sock.getsockname()[1]}"


@asynccontextmanager
async def _serving(app: ASGIApp, sock: socket.socket) -> AsyncIterator[None]:
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        while not server.started:
            await anyio.sleep(0.01)
        try:
            yield
        finally:
            server.should_exit = True
