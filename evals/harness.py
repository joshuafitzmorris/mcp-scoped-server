"""Running golden cases: load them, give an agent a tool session as the case's user, record what happened.

The session calls tools through the same `invoke` the MCP server uses, so scopes,
tenant scoping and the result check all apply. The agent only ever sees the
tools that user's scopes allow, with the schemas the server publishes.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic_core import to_jsonable_python

from scoped_mcp.registry import Principal, ToolFailure, invoke, registry
from scoped_mcp.server import build_server
from scoped_mcp.store import Store

GOLDEN = Path(__file__).with_name("golden.toml")


@dataclass(frozen=True)
class Case:
    id: str
    split: Literal["dev", "heldout"]
    user: str
    query: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    answer_contains: list[str | list[str]] = field(default_factory=list)
    total_cents: int | None = None
    state: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    refuse: bool = False


@dataclass(frozen=True)
class Call:
    tool: str
    args: dict[str, Any]
    ok: bool
    output: Any  # the JSON-shaped result, or the error message


class ToolSession:
    def __init__(self, principal: Principal, store: Store, schemas: dict[str, dict[str, Any]]):
        self.principal = principal
        self.store = store
        self._schemas = schemas
        self.trace: list[Call] = []

    def tools(self) -> list[dict[str, Any]]:
        return [self._schemas[t.name] for t in registry.visible_to(self.principal)]

    def call(self, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
        try:
            ok, output = True, to_jsonable_python(invoke(registry, name, self.principal, dict(args), self.store))
        except ToolFailure as e:
            ok, output = False, str(e)
        self.trace.append(Call(name, dict(args), ok, output))
        return ok, output


class Agent(Protocol):
    name: str

    def run(self, query: str, session: ToolSession) -> str: ...


@dataclass(frozen=True)
class Run:
    case: Case
    trace: list[Call]
    answer: str
    store: Store  # after the run, for state checks


def load_cases(path: Path = GOLDEN) -> list[Case]:
    raw = tomllib.loads(path.read_text())["case"]
    return [Case(**c) for c in raw]


async def tool_schemas() -> dict[str, dict[str, Any]]:
    """What the MCP server publishes for each tool, so the agent sees what an MCP client would."""
    server = build_server(registry=registry, store=Store.from_fixtures(), auth=None)
    return {
        t.name: {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
        for t in await server.list_tools()
    }


def run_case(agent: Agent, case: Case, schemas: dict[str, dict[str, Any]]) -> Run:
    store = Store.from_fixtures()
    user = store.user(case.user)
    if user is None:
        raise ValueError(f"case {case.id}: no fixture user {case.user}")
    session = ToolSession(Principal(user.subject, user.tenant, frozenset(user.scopes)), store, schemas)
    answer = agent.run(case.query, session)
    return Run(case, session.trace, answer, store)
