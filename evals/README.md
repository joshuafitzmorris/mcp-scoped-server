# Evals

Eleven hand-written questions in `golden.toml`, each asked as one of the test users. For each case the harness gives an agent the tools that user can see, records every call through the same `invoke` path the MCP server uses, and scores the run.

```sh
uv run python -m evals                                   # dev cases, baseline agent
uv run python -m evals --split all --gate                # what CI runs
uv run --extra llm python -m evals --agent claude        # Claude, needs Anthropic credentials
uv run --extra llm python -m evals --agent claude --model claude-sonnet-5-5 --split heldout
```

## What a case checks

- **Calls**: each expected call appears in the trace. Only the arguments the case lists are compared, and a case can accept more than one tool, because two routes to the same answer are both fine.
- **Results**: some combination of facts the answer must contain, a total some call must have returned, invoice statuses after the run, an error some call must have hit, or no records at all.
- **Leaks**: every tool output and the final answer are scanned for another tenant's ids, memos and contract titles. Text the user typed doesn't count, so asking for INV-1001 by name and getting "not found" back is clean.

A case passes when every check it declares passes. `--gate` exits 1 if any case leaks, whatever the pass rate.

## Why eleven cases need a held-out split

Small golden sets lie in two ways.

**Noise.** Seven dev cases means one case is fourteen points. A perfect 7/7 has a 95% interval of 65% to 100%. A prompt change that flips one case tells you very little, so the report prints counts and intervals and no percentage to one decimal place.

**Fitting.** Each time you read a failing case and adjust a prompt or a tool description until it passes, that case measures the agent a little less and your memory of the case a little more. After enough rounds the dev cases are training data. The only cases still measuring anything are the ones you haven't tuned against.

The baseline here shows the second problem in miniature. I wrote `KeywordAgent`'s rules by reading the seven dev cases and nothing else. It passes all seven. The four held-out cases ask for the same kinds of things in other words ("close out INV-1005" where dev says "mark INV-1005 as paid"), and it passes two:

```
split      passed   95% interval
dev        7/7      65% to 100%
heldout    2/4      15% to 85%
leaks      0 of 11 cases

failures
  heldout  customer-paraphrase                  expected search_invoices|list_invoices(); made no calls
  heldout  customer-paraphrase                  answer lacks 'INV-1001'
  heldout  customer-paraphrase                  answer lacks 'INV-1005'
  heldout  mark-paid-paraphrase                 expected mark_invoice_paid(invoice_id='INV-1005'); made get_invoice(invoice_id='INV-1005')
  heldout  mark-paid-paraphrase                 INV-1005 is open, expected paid
```

Nothing in its dev score predicted that. A model-backed agent fails less crudely, but tuning a system prompt against seven cases overfits the same way.

What I do with a set this size:

- Hold out about a third, and run it at checkpoints (before a release, after a model change), not on every edit.
- Don't read held-out failures while you are still tuning. Once you have read them, move those cases into dev and write fresh held-out ones.
- Grow both splits from real failures in traces, not from what you imagine users ask.
- Gate on invariants and report accuracy. A leak is wrong at a sample size of one, so it fails CI. An accuracy drop of one case is noise at this size, so it doesn't.

Here the held-out cases sit in the same file as the dev cases, which is fine for a demo. On a real project keep them where the person tuning can't read them casually.

## What the scorer doesn't check

- Answer quality beyond required substrings. There is no LLM judge; a judge is another model whose errors you would then need to measure.
- Numbers derived from another tenant's data. The leak scan sees ids and text. The `total_cents` check covers the one aggregate in the set, and the noninterference test in CI covers the tools themselves.
- Blind spots I share with the cases. I wrote all eleven.

## ClaudeAgent

A plain tool-use loop over the Messages API (`claude-opus-5-5` by default, `--model` to change it). It isn't run in CI, because it spends money: a full run is about thirty API calls on your own key. It has no refusal fallback on purpose: an eval has to know which model produced each answer, so a refusal is recorded as `[refused]` and scored like any other answer.
