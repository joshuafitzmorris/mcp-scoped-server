"""Run the golden cases.

    uv run python -m evals                          # dev cases, baseline agent
    uv run python -m evals --split all --gate       # what CI runs: exit 1 on any leak
    uv run --extra llm python -m evals --agent claude --split heldout
"""

import argparse
import sys

import anyio

import scoped_mcp.tools  # noqa: F401  registers the tools
from evals.agents import ClaudeAgent, KeywordAgent
from evals.harness import load_cases, run_case, tool_schemas
from evals.score import report, score


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("--agent", choices=["keyword", "claude"], default="keyword")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    parser.add_argument("--gate", action="store_true", help="exit 1 if any case leaks another tenant's data")
    args = parser.parse_args()

    agent = KeywordAgent() if args.agent == "keyword" else ClaudeAgent(model=args.model)
    cases = [c for c in load_cases() if args.split in ("all", c.split)]
    schemas = anyio.run(tool_schemas)
    scores = [score(run_case(agent, case, schemas)) for case in cases]
    print(report(agent.name, scores))
    return 1 if args.gate and any(s.leaks for s in scores) else 0


if __name__ == "__main__":
    sys.exit(main())
