"""Putting the registry on an MCP server.

`build_server` exposes every registered tool through `invoke`, and installs
`ScopeGate`, a middleware that sees every tools/list and tools/call before the
SDK handles it:

- tools/list shows the caller only the tools their scopes allow. That is for
  the model's benefit, so it doesn't plan around tools it can't use. Hiding a
  tool is not what stops it being called.
- tools/call is refused for a tool the registry doesn't know, or whose scopes
  the caller lacks, before arguments are even validated. A tool that reached
  the MCP server some other way can't be called.

`checked_app` won't hand back an ASGI app while the server has a tool the
registry doesn't know, so a server with an undeclared tool doesn't start.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from starlette.applications import Starlette

from scoped_mcp.registry import Denied, Principal, Registry, ScopedTool, ToolFailure, authorize, invoke
from scoped_mcp.store import Store

INSTRUCTIONS = (
    "Every tool here answers for the signed-in user's own organisation and nothing else. "
    "If the user asks about another organisation's records, say you can't see them."
)


class UndeclaredTool(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerAuth:
    issuer_url: str
    resource_url: str
    verifier: TokenVerifier


def current_principal() -> Principal | None:
    """The caller, from the verified access token. The tenant comes from the token, never from a tool argument."""
    token = get_access_token()
    if token is None or not token.subject:
        return None
    tenant = (token.claims or {}).get("tenant")
    if not isinstance(tenant, str) or not tenant:
        return None
    return Principal(subject=token.subject, tenant=tenant, scopes=frozenset(token.scopes))


class ScopeGate:
    def __init__(self, registry: Registry):
        self._registry = registry

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            try:
                authorize(self._registry.get(name), current_principal(), name)
            except Denied as e:
                return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
        result = await call_next(ctx)
        if ctx.method == "tools/list" and isinstance(result, dict):
            visible = {t.name for t in self._registry.visible_to(current_principal())}
            tools = [t for t in result.get("tools", []) if t.get("name") in visible]
            # The list differs per caller, so no cache may share it between callers.
            result = {**result, "tools": tools, "cacheScope": "private"}
        return result


def build_server(*, registry: Registry, store: Store, auth: ServerAuth | None) -> MCPServer:
    """With `auth=None` the server has no way to authenticate anyone, so it lists nothing and refuses every call."""
    mcp = MCPServer(
        name="scoped-mcp",
        instructions=INSTRUCTIONS,
        token_verifier=auth.verifier if auth else None,
        auth=AuthSettings(
            issuer_url=auth.issuer_url,
            resource_server_url=auth.resource_url,
            validate_token_resource=True,
        )
        if auth
        else None,
        middleware=[ScopeGate(registry)],
        log_level="WARNING",
    )
    for t in registry:
        mcp.add_tool(
            _expose(t, registry, store),
            name=t.name,
            description=t.description,
            annotations=ToolAnnotations(read_only_hint=not t.writes, open_world_hint=False),
        )
    return mcp


async def undeclared_tools(mcp: MCPServer, registry: Registry) -> list[str]:
    return sorted({t.name for t in await mcp.list_tools()} - {t.name for t in registry})


async def checked_app(mcp: MCPServer, registry: Registry) -> Starlette:
    if undeclared := await undeclared_tools(mcp, registry):
        raise UndeclaredTool(f"refusing to start: tools without a scope declaration: {', '.join(undeclared)}")
    return mcp.streamable_http_app()


def _expose(tool: ScopedTool, registry: Registry, store: Store) -> Callable[..., Any]:
    """The function the SDK sees: the tool's own parameters minus the TenantView, routed through `invoke`."""

    async def call(**kwargs: Any) -> Any:
        try:
            return invoke(registry, tool.name, current_principal(), kwargs, store)
        except ToolFailure as e:
            raise ToolError(str(e)) from None

    call.__name__ = tool.name
    call.__doc__ = tool.description
    call.__signature__ = tool.signature  # type: ignore[attr-defined]
    call.__annotations__ = {p.name: p.annotation for p in tool.signature.parameters.values()}
    call.__annotations__["return"] = tool.signature.return_annotation
    return call
