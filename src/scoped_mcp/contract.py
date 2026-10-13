"""The tool contract: what CI checks about every registered tool.

1. Declared. Every tool on the MCP server came through the registry, and every
   registered tool declares one or more scopes from the catalog.
2. No back doors. A module that defines a tool doesn't import the raw store,
   doesn't touch a private attribute (`view._store`), and doesn't use the
   builtins that get around both. This is a lint for honest mistakes, not a sandbox.
3. Noninterference. For every tool, every tenant, and a sweep of arguments
   built from the tool's type hints, the result is identical whether or not
   other tenants' rows exist, and no other tenant's rows change. This catches
   a leak however the tool was written, including through an aggregate or an
   error message, but only for arguments the sweep tried and only through the
   store it was handed.

Each check returns a list of violations; the tests assert the lists are empty.
"""

import ast
import importlib.util
import inspect
import itertools
import random
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic_core import to_jsonable_python

from scoped_mcp.registry import SCOPES, Principal, Registry, ScopedTool, ToolFailure, invoke
from scoped_mcp.server import undeclared_tools
from scoped_mcp.store import Store

FORBIDDEN_MODULES = ("scoped_mcp.store", "importlib")
FORBIDDEN_NAMES = {"Store"}
FORBIDDEN_CALLS = {"__import__", "eval", "exec", "compile", "getattr", "setattr", "delattr", "vars", "globals"}
MAX_PROBES_PER_TOOL = 2000


@dataclass(frozen=True)
class Violation:
    tool: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.tool}: {self.rule}: {self.detail}"


# 1. Declared


async def server_declarations(mcp: MCPServer, registry: Registry) -> list[Violation]:
    return [
        Violation(name, "undeclared", "on the MCP server without a scope declaration from the registry")
        for name in await undeclared_tools(mcp, registry)
    ]


def scope_declarations(registry: Registry) -> list[Violation]:
    out = []
    for t in registry:
        if not t.scopes:
            out.append(Violation(t.name, "undeclared", "declares no scopes"))
        elif unknown := t.scopes - SCOPES.keys():
            out.append(Violation(t.name, "unknown scope", ", ".join(sorted(unknown))))
    return out


# 2. No back doors


def back_doors(registry: Registry) -> list[Violation]:
    out = []
    modules: dict[str, list[str]] = {}
    for t in registry:
        modules.setdefault(t.fn.__module__, []).append(t.name)
    for module, tool_names in sorted(modules.items()):
        path = inspect.getsourcefile(registry.get(tool_names[0]).fn)  # type: ignore[union-attr]
        if path is None:
            out.append(Violation(module, "back door", "source not found, so it can't be checked"))
            continue
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        package = module.rpartition(".")[0]
        for node in ast.walk(tree):
            for problem in _back_door(node, package):
                out.append(Violation(module, "back door", f"line {node.lineno}: {problem}"))  # type: ignore[attr-defined]
    return out


def _back_door(node: ast.AST, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [f"imports {a.name}" for a in node.names if _forbidden_module(a.name)]
    if isinstance(node, ast.ImportFrom):
        base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
        bad = [
            a.name
            for a in node.names
            if _forbidden_module(base) or _forbidden_module(f"{base}.{a.name}") or a.name in FORBIDDEN_NAMES
        ]
        return [f"imports {', '.join(bad)} from {base}"] if bad else []
    if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
        return [f"touches private attribute .{node.attr}"]
    if isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
        return [f"uses {node.id}()"]
    return []


def _forbidden_module(name: str) -> bool:
    return any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN_MODULES)


# 3. Noninterference


class Unprobeable(Exception):
    pass


def noninterference(registry: Registry, load: Callable[[], Store] = Store.from_fixtures) -> list[Violation]:
    base = load()
    pool = _probe_pool(base)
    out = []
    for t in registry:
        try:
            probes = _probes(t, pool)
        except Unprobeable as e:
            out.append(Violation(t.name, "unprobeable", f"{e}; add probe values for that type in contract.py"))
            continue
        for tenant in base.tenants():
            caller = Principal(subject=f"probe@{tenant}", tenant=tenant, scopes=t.scopes)
            for args in probes:
                shared, alone = load(), load().only(tenant)
                before = shared.snapshot_except(tenant)
                with_others = _outcome(registry, t.name, caller, args, shared)
                without = _outcome(registry, t.name, caller, args, alone)
                if with_others != without:
                    out.append(
                        Violation(
                            t.name,
                            "reads another tenant",
                            f"as {tenant} with {args}: {_brief(without)} alone, {_brief(with_others)} with other tenants present",
                        )
                    )
                    break
                if shared.snapshot_except(tenant) != before:
                    out.append(Violation(t.name, "writes another tenant", f"as {tenant} with {args}"))
                    break
    return out


def _outcome(registry: Registry, name: str, caller: Principal, args: dict[str, Any], store: Store) -> tuple:
    try:
        return ("ok", to_jsonable_python(invoke(registry, name, caller, args, store)))
    except ToolFailure as e:
        return ("error", type(e).__name__, str(e))
    except Exception as e:  # a crash that depends on other tenants' rows is interference too
        return ("crash", type(e).__name__)


def _brief(outcome: tuple) -> str:
    text = repr(outcome[1:] if outcome[0] != "ok" else outcome[1])
    return f"{outcome[0]} {text[:120]}{'...' if len(text) > 120 else ''}"


def _probe_pool(store: Store) -> dict[type, list[Any]]:
    """Argument values to try: every id, name and text fragment in the fixtures, from every tenant, plus misses."""
    strings: set[str] = {"", "INV-0000", "CTR-0000"}
    for i in store.invoices:
        strings |= {i.id, i.customer, i.memo, i.memo.split()[0]}
    for c in store.contracts:
        strings |= {c.id, c.counterparty, c.title}
    return {str: sorted(strings), int: [-1, 0, 1, 30, 365], bool: [False, True]}


def _probes(tool: ScopedTool, pool: dict[type, list[Any]]) -> list[dict[str, Any]]:
    names = list(tool.signature.parameters)
    choices = [_values(p.annotation, pool, f"{tool.name}({p.name})") for p in tool.signature.parameters.values()]
    combos = list(itertools.product(*choices))
    if len(combos) > MAX_PROBES_PER_TOOL:
        combos = random.Random(tool.name).sample(combos, MAX_PROBES_PER_TOOL)
    return [dict(zip(names, combo, strict=True)) for combo in combos]


def _values(annotation: Any, pool: dict[type, list[Any]], where: str) -> list[Any]:
    origin = typing.get_origin(annotation)
    if origin is Literal:
        return list(typing.get_args(annotation))
    if origin in (typing.Union, types.UnionType):
        values: list[Any] = []
        for arm in typing.get_args(annotation):
            values += [None] if arm is type(None) else _values(arm, pool, where)
        return values
    if annotation in pool:
        return pool[annotation]
    raise Unprobeable(f"{where}: no probe values for {annotation!r}")
