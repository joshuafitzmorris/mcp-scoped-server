"""The eval harness: the scorer, the leak gate, and the Claude loop against a fake client."""

import re
from types import SimpleNamespace

import anyio

from evals.agents import ClaudeAgent, KeywordAgent
from evals.harness import Case, Run, ToolSession, load_cases, run_case, tool_schemas
from evals.score import score, wilson
from scoped_mcp.store import Store

SCHEMAS = anyio.run(tool_schemas)


class Scripted:
    """Makes fixed calls, then answers with fixed text."""

    name = "scripted"

    def __init__(self, calls: list[tuple[str, dict]], answer: str = ""):
        self.calls, self.answer = calls, answer

    def run(self, query: str, session: ToolSession) -> str:
        for name, args in self.calls:
            session.call(name, args)
        return self.answer


def test_the_golden_set_loads_with_a_held_out_split() -> None:
    cases = load_cases()
    assert len(cases) >= 10
    assert {c.split for c in cases} == {"dev", "heldout"}
    assert len({c.id for c in cases}) == len(cases)


def test_a_run_that_meets_every_check_passes() -> None:
    case = Case("t", "dev", "ana@quillmoor.example", "Mark INV-1005 as paid.",
                calls=[{"tool": "mark_invoice_paid", "args": {"invoice_id": "inv-1005"}}],
                state={"INV-1005": "paid"}, answer_contains=[["done", "paid"]])
    result = score(run_case(Scripted([("mark_invoice_paid", {"invoice_id": "INV-1005"})], "Paid."), case, SCHEMAS))
    assert result.passed, result.failures


def test_each_check_reports_its_own_failure() -> None:
    case = Case("t", "dev", "ana@quillmoor.example", "How much is overdue?",
                calls=[{"tool": "invoice_totals"}], total_cents=571000, answer_contains=["5,710"])
    result = score(run_case(Scripted([("list_invoices", {})], "No idea."), case, SCHEMAS))
    assert [f.split(" ")[0] for f in result.failures] == ["expected", "answer", "expected"]


def test_another_tenants_text_in_the_answer_is_a_leak_unless_the_user_typed_it() -> None:
    case = Case("t", "dev", "cora@tarnwick.example", "Pull up invoice INV-1001.")
    store = Store.from_fixtures()
    leaked = score(Run(case, [], "INV-1001 is for Etched glass panels for the front counter", store))
    assert leaked.leaks == ["Etched glass panels for the front counter"]


def test_an_agent_that_asks_for_every_id_still_gets_only_its_own_tenant() -> None:
    every_id = [("get_invoice", {"invoice_id": f"INV-{n}"}) for n in range(1001, 1011)]
    every_id += [("get_contract", {"contract_id": f"CTR-{n}"}) for n in range(2001, 2006)]
    case = Case("t", "dev", "cora@tarnwick.example", "Show me everything.")
    result = score(run_case(Scripted(every_id), case, SCHEMAS))
    assert result.leaks == []


def test_the_baseline_passes_every_dev_case() -> None:
    # guards: the dev split drifting away from the baseline written against it, which would make the README's numbers wrong
    dev = [c for c in load_cases() if c.split == "dev"]
    assert all(score(run_case(KeywordAgent(), c, SCHEMAS)).passed for c in dev)


def test_wilson_intervals_are_as_wide_as_small_samples_deserve() -> None:
    low, high = wilson(7, 7)
    assert round(low, 2) == 0.65 and high == 1.0
    assert tuple(round(x, 2) for x in wilson(2, 4)) == (0.15, 0.85)


class FakeMessages:
    """Answers like the Messages API: first a tool_use block per requested call, then text."""

    def __init__(self, calls: list[tuple[str, dict]]):
        self.calls, self.requests = calls, []

    def create(self, **request):
        self.requests.append(request)
        if len(self.requests) == 1:
            blocks = [SimpleNamespace(type="tool_use", id=f"toolu_{i}", name=n, input=a) for i, (n, a) in enumerate(self.calls)]
            return SimpleNamespace(stop_reason="tool_use", content=blocks)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done.")])


def test_the_claude_loop_returns_every_tool_result_and_marks_errors() -> None:
    fake = FakeMessages([("list_invoices", {"status": "open"}), ("mark_invoice_paid", {"invoice_id": "INV-1005"})])
    agent = ClaudeAgent(client=SimpleNamespace(messages=fake))
    case = Case("t", "dev", "ben@quillmoor.example", "Pay the open ones.")
    run = run_case(agent, case, SCHEMAS)

    assert run.answer == "Done."
    offered = {t["name"] for t in fake.requests[0]["tools"]}
    assert "mark_invoice_paid" not in offered  # ben can't see it
    results = fake.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_0", "toolu_1"]
    assert [r["is_error"] for r in results] == [False, True]
    assert re.search("needs scope invoices:write", results[1]["content"])


def test_the_claude_loop_reports_a_refusal() -> None:
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: SimpleNamespace(stop_reason="refusal", content=[])))
    case = Case("t", "dev", "ana@quillmoor.example", "Hello")
    assert run_case(ClaudeAgent(client=client), case, SCHEMAS).answer == "[refused]"
