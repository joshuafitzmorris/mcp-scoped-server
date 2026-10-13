"""Declaring tools and calling them.

A tool is registered with the scopes it needs. Registration fails without
them, the server refuses to start with a tool that skipped registration, and
every call goes through `authorize` and then `invoke`:

    unknown tool              -> denied
    no authenticated caller   -> denied
    caller lacks a scope      -> denied
    otherwise                 -> the tool runs against a TenantView for the
                                 caller's tenant, and its result is checked for
                                 other tenants' records before it is returned
"""

import inspect
import logging
import typing
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from scoped_mcp.tenancy import TenantView

if TYPE_CHECKING:
    from scoped_mcp.store import Store

log = logging.getLogger("scoped_mcp.audit")

# The whole scope catalog. The issuer grants from it and the contract checks every declaration against it.
SCOPES: dict[str, str] = {
    "profile:read": "See who you are signed in as",
    "invoices:read": "Read your organisation's invoices",
    "invoices:write": "Change an invoice's status",
    "contracts:read": "Read your organisation's contracts",
}


class ToolFailure(Exception):
    """A failure the model may read. The message must never contain another tenant's data."""


class Denied(ToolFailure):
    """The caller may not make this call."""


class NotFound(ToolFailure):
    """Nothing with that id is visible to the caller. Also what another tenant's id gets."""


class CrossTenantResult(Denied):
    """A tool tried to return another tenant's record. Always a bug in the tool."""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class ScopedTool:
    name: str
    fn: Callable[..., Any]
    scopes: frozenset[str]
    description: str
    signature: inspect.Signature  # the model-facing parameters: the TenantView is not one of them

    @property
    def writes(self) -> bool:
        return any(s.endswith(":write") for s in self.scopes)


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, ScopedTool] = {}

    def tool(self, *, scopes: Iterable[str]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a tool. `scopes` is keyword-only and required, so leaving it out is a TypeError at import."""
        declared = frozenset(scopes)
        # An empty list is what someone types to make the error go away, so it is refused like a missing one.
        if not declared:
            raise ValueError("a tool must declare at least one scope")
        unknown = declared - SCOPES.keys()
        if unknown:
            raise ValueError(f"unknown scope(s): {', '.join(sorted(unknown))}")

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add(ScopedTool(fn.__name__, fn, declared, inspect.getdoc(fn) or "", _model_facing_signature(fn)))
            return fn

        return register

    def add(self, tool: ScopedTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: object) -> ScopedTool | None:
        return self._tools.get(name) if isinstance(name, str) else None

    def visible_to(self, principal: Principal | None) -> list[ScopedTool]:
        if principal is None:
            return []
        return [t for t in self if t.scopes <= principal.scopes]

    def __iter__(self) -> Iterator[ScopedTool]:
        return iter(sorted(self._tools.values(), key=lambda t: t.name))

    def __len__(self) -> int:
        return len(self._tools)


def _model_facing_signature(fn: Callable[..., Any]) -> inspect.Signature:
    hints = typing.get_type_hints(fn)
    params = list(inspect.signature(fn).parameters.values())
    if not params or hints.get(params[0].name) is not TenantView:
        raise TypeError(f"{fn.__name__}: the first parameter must be annotated `TenantView`")
    return inspect.Signature(
        [p.replace(annotation=hints.get(p.name, p.annotation)) for p in params[1:]],
        return_annotation=hints.get("return", inspect.Signature.empty),
    )


def authorize(tool: ScopedTool | None, principal: Principal | None, name: object) -> ScopedTool:
    if tool is None:
        _audit("deny", principal, name, "undeclared tool")
        raise Denied(f"{name} is not a declared tool")
    if principal is None:
        _audit("deny", principal, name, "no authenticated caller")
        raise Denied("no authenticated caller")
    missing = tool.scopes - principal.scopes
    if missing:
        _audit("deny", principal, name, f"missing {sorted(missing)}")
        raise Denied(f"{tool.name} needs scope {', '.join(sorted(missing))}")
    return tool


def invoke(registry: Registry, name: str, principal: Principal | None, args: dict[str, Any], store: "Store") -> Any:
    tool = authorize(registry.get(name), principal, name)
    assert principal is not None
    try:
        tool.signature.bind(**args)
    except TypeError as e:
        raise ToolFailure(f"bad arguments for {tool.name}: {e}") from None
    view = TenantView(store, tenant=principal.tenant, subject=principal.subject, scopes=principal.scopes)
    result = tool.fn(view, **args)
    if foreign := _foreign_tenants(result, principal.tenant):
        _audit("withheld", principal, name, f"result held records of {sorted(foreign)}")
        log.error("tool %s returned records from other tenants; fix the tool", name)
        raise CrossTenantResult(f"{tool.name} returned a record outside your organisation; result withheld")
    _audit("allow", principal, name, "")
    return result


def _foreign_tenants(value: Any, tenant: str) -> set[str]:
    """Tenants other than `tenant` that own any record inside `value`. Sees records, not numbers derived from them."""
    found: set[str] = set()
    if isinstance(value, BaseModel):
        owner = getattr(value, "tenant", tenant)
        if owner != tenant:
            found.add(owner)
        for field in type(value).model_fields:
            found |= _foreign_tenants(getattr(value, field), tenant)
    elif isinstance(value, dict):
        for item in value.values():
            found |= _foreign_tenants(item, tenant)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            found |= _foreign_tenants(item, tenant)
    return found


def _audit(decision: str, principal: Principal | None, name: object, reason: str) -> None:
    who = f"{principal.subject}@{principal.tenant}" if principal else "anonymous"
    log.info("%s %s %s %s", decision, who, name, reason)


registry = Registry()
tool = registry.tool
