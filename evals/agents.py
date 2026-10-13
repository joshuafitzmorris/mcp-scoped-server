"""Agents the harness can run.

KeywordAgent is a deterministic baseline, so the harness, the scorer and the
leak gate run in CI with no API key. I wrote its rules by reading the dev cases
and nothing else. It does well on dev and worse on held-out, and that gap is
what a held-out set is for.

ClaudeAgent is the real thing: Claude with the tools the case's user can see,
in a plain tool-use loop. It needs `uv sync --extra llm` and Anthropic credentials.
"""

import json
import re
from typing import Any

from evals.harness import ToolSession
from scoped_mcp.server import INSTRUCTIONS


class KeywordAgent:
    name = "keyword (baseline: rules written against the dev cases)"

    def run(self, query: str, session: ToolSession) -> str:
        q = query.lower()
        invoice_id = re.search(r"INV-\d{4}", query)
        status = next((s for s in ("open", "paid", "overdue") if s in q), None)
        if "who am i" in q or "signed in" in q:
            name, args = "whoami", {}
        elif invoice_id and "mark" in q and "paid" in q:
            name, args = "mark_invoice_paid", {"invoice_id": invoice_id.group()}
        elif invoice_id:
            name, args = "get_invoice", {"invoice_id": invoice_id.group()}
        elif "contract" in q:
            name, args = "list_contracts", {}
        elif "how much" in q or "total" in q:
            name, args = "invoice_totals", {"status": status}
        elif "invoice" in q:
            name, args = "list_invoices", {"status": status}
        else:
            return "I don't know how to help with that."
        ok, output = session.call(name, args)
        return describe(output) if ok else f"I couldn't do that: {output}"


def describe(output: Any) -> str:
    if isinstance(output, dict) and "result" in output:
        output = output["result"]
    if isinstance(output, list):
        return "\n".join(describe(o) for o in output) or "Nothing matched."
    if "customer" in output:
        return f"{output['id']}  {output['customer']}  {output['status']}  {_dollars(output['amount_cents'])}  due {output['due']}"
    if "counterparty" in output:
        return f"{output['id']}  {output['counterparty']}  {output['title']}  {output['status']}  ends {output['ends']}"
    if "total_cents" in output:
        return f"{output['count']} invoices, {_dollars(output['total_cents'])} in total"
    if "subject" in output:
        return f"You are signed in as {output['subject']} at {output['tenant_name']}."
    return json.dumps(output)


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


class ClaudeAgent:
    def __init__(self, model: str = "claude-opus-5-5", max_turns: int = 8, client: Any = None):
        if client is None:
            import anthropic  # optional dependency: uv sync --extra llm

            client = anthropic.Anthropic()
        self._client = client
        self._model = model
        self._max_turns = max_turns
        self.name = f"claude ({model})"

    def run(self, query: str, session: ToolSession) -> str:
        tools = session.tools()
        messages: list[dict[str, Any]] = [{"role": "user", "content": query}]
        for _ in range(self._max_turns):
            response = self._client.messages.create(
                model=self._model,
                max_tokens=16000,
                system=INSTRUCTIONS,
                messages=messages,
                **({"tools": tools} if tools else {}),
            )
            # No refusal fallback on purpose: an eval has to know which model produced the answer.
            if response.stop_reason == "refusal":
                return "[refused]"
            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text")
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    ok, output = session.call(block.name, block.input)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(output) if ok else output,
                            "is_error": not ok,
                        }
                    )
            messages.append({"role": "user", "content": results})
        return "[stopped after too many turns]"
