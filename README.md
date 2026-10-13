# mcp-scoped-server

A reference MCP server where every tool declares the scopes it needs, every result is narrowed to the caller's tenant, and CI fails the build for a tool that does neither. The data is fake: two invented companies' invoices and contracts, eight tools, three test users.

I built this pattern in production for a multi-tenant document platform with 40 tools. This repo is a from-scratch reimplementation on fake data, small enough to read in one sitting.

## The threat: an agent is a confused deputy

A confused deputy is a program that holds authority and lets someone else decide how it is used. An agent with tools is one by construction. It acts with the signed-in user's authority, and it takes instructions from the user, from the documents it reads, and from what the tools return. Prompt injection is the someone else.

In a multi-tenant system that goes wrong in a few specific ways:

- The model passes another tenant's id, found in a pasted email, guessed, or planted in a document, and the tool fetches it.
- A tool returns more than the caller may see: a total across every tenant, or an error that confirms another tenant's id exists.
- Someone adds a tool in a hurry with no permission check at all.
- A token issued for one service is replayed against another.

The rule that handles most of this: **the model never chooses whose data it touches.** No tool takes a tenant argument. The tenant comes from the verified access token, and everything the model does control (which tool, which arguments) is checked against it.

## Run it

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest                          # 48 tests, about 3 seconds
uv run python -m scoped_mcp.demo       # issuer + server on loopback, three users signed in with PKCE
uv run python -m evals --split all     # the golden cases against the baseline agent
```

The demo starts the test issuer and the MCP server, signs each user in the way an MCP client would, and calls tools over streamable HTTP with the SDK's own client:

```
no token: HTTP 401, Bearer error="invalid_token", error_description="Authentication required", resource_metadata="http://127.0.0.1:53507/.well-known/oauth-protected-resource/mcp"

ana@quillmoor.example  tenant=quillmoor  scopes=contracts:read invoices:read invoices:write profile:read
  sees 8 tools: get_contract, get_invoice, invoice_totals, list_contracts, list_invoices, mark_invoice_paid, search_invoices, whoami
  list_invoices(status='open') -> INV-1001 Harbor Lane Cafe open, INV-1005 Harbor Lane Cafe open
  get_invoice(invoice_id='INV-1002') -> error: Error executing tool get_invoice: invoice not found
  invoice_totals() -> {"tenant": "quillmoor", "status": null, "count": 5, "total_cents": 1088500}

ben@quillmoor.example  tenant=quillmoor  scopes=invoices:read profile:read
  sees 5 tools: get_invoice, invoice_totals, list_invoices, search_invoices, whoami
  mark_invoice_paid(invoice_id='INV-1005') -> error: mark_invoice_paid needs scope invoices:write

cora@tarnwick.example  tenant=tarnwick  scopes=contracts:read invoices:read invoices:write profile:read
  sees 8 tools: get_contract, get_invoice, invoice_totals, list_contracts, list_invoices, mark_invoice_paid, search_invoices, whoami
  search_invoices(query='Harbor Lane') -> INV-1010 Harbor Lane Cafe paid
  invoice_totals() -> {"tenant": "tarnwick", "status": null, "count": 5, "total_cents": 403000}
```

INV-1002 is Tarnwick's, so to Ana it doesn't exist. Harbor Lane Cafe is a customer of both companies; Cora's search finds only Tarnwick's invoice.

## Design

### A tool declares its scopes and gets a tenant view

```python
@tool(scopes=["invoices:read"])
def get_invoice(view: TenantView, invoice_id: str) -> Invoice:
    """Fetch one invoice by id, for example INV-1001."""
    invoice = view.invoice(invoice_id)
    if invoice is None:
        raise NotFound("invoice not found")
    return invoice
```

`scopes` is a required keyword, so leaving it out is a TypeError at import. An empty list and an unknown scope are refused too. The first parameter must be a `TenantView`: the only data handle a tool gets, already narrowed to the caller's tenant, handing out copies. The model sees `invoice_id` and nothing else. Another tenant's id gets the same error as a typo, because a different error would tell the caller the id exists.

### Every call goes through one path

| When | What says no | What it refuses |
|---|---|---|
| Import | `Registry.tool` | a tool with no scopes, empty scopes or unknown scopes, or no `TenantView` first parameter |
| Start | `checked_app` | any tool on the MCP server that didn't come through the registry |
| `tools/list` | `ScopeGate` middleware | nothing; it hides tools the caller's scopes don't allow, so the model doesn't plan around them |
| `tools/call` | `ScopeGate`, then `invoke` | an undeclared tool, a caller with no valid token, a missing scope; checked before the SDK validates arguments |
| Result | `invoke` | a result holding a record owned by another tenant; it is withheld and logged as a bug |

The list filter is for the model's benefit. The call path checks scopes whether or not the tool was listed. The per-caller tool list is marked `cacheScope: private` so no cache shares it between users.

### Auth: a real PKCE flow against a local test issuer

`scoped_mcp/auth/issuer.py` is a minimal OAuth 2.1 authorization server that runs on loopback for the demo and the tests. It is a test issuer, and it is test-only in one respect: there is no login page, and `/authorize` signs in whichever test user `login_hint` names. Everything after that is checked the way a real server should:

- the client and its exact redirect URI must be registered; an unknown redirect URI gets an error page, never a redirect
- PKCE is required, S256 only; the token endpoint recomputes the challenge from the verifier
- codes are single use (spent by the first attempt, even a failed one) and expire after 60 seconds
- the `resource` parameter (RFC 8707) is required and becomes the token's audience
- granted scopes are what the client asked for intersected with what the user holds
- the redirect carries `iss` (RFC 9207), and the client checks it along with `state`

The MCP server is a resource server. `JwtVerifier` implements the SDK's `TokenVerifier`: ES256 only (so `none` and HMAC tokens fail), issuer and audience must match, 30 seconds of clock skew, and every claim the call path needs must be present. A token minted for another resource is refused, which is how token passthrough is actually stopped. Any issuer that publishes a JWKS and puts `sub`, `scope` and a tenant claim in its access tokens can replace the test issuer without touching the server.

`scoped_mcp/auth/client.py` is the client half: the 401, protected resource metadata (RFC 9728), authorization server metadata (RFC 8414), then PKCE.

### Layout

| Path | What |
|---|---|
| `src/scoped_mcp/registry.py` | the scope catalog, `@tool`, `authorize`, `invoke`, the result check |
| `src/scoped_mcp/tenancy.py` | `TenantView` |
| `src/scoped_mcp/server.py` | `build_server`, `ScopeGate`, `checked_app` |
| `src/scoped_mcp/contract.py` | the checks CI runs |
| `src/scoped_mcp/tools/` | the eight tools |
| `src/scoped_mcp/auth/` | test issuer, verifier, PKCE, client flow |
| `src/scoped_mcp/store.py`, `fixtures.json` | the raw store and the fake data; only `tenancy.py` and the contract read the store |
| `tests/test_contract.py` | the signature test |
| `tests/broken/tools.py` | tools that break the contract on purpose |
| `evals/` | golden cases, harness, scorer, agents |

Built on the official MCP Python SDK 2.x, where FastMCP is now `MCPServer`.

## What CI guarantees

`.github/workflows/ci.yml` runs the tool contract as its own step, then every test, then the evals with a gate on leaks. The contract is `tests/test_contract.py`, backed by `src/scoped_mcp/contract.py`:

1. **Declared.** Every tool on the MCP server came through the registry, and every registered tool declares one or more scopes from the catalog.
2. **No back doors.** No module that defines a tool imports the raw store, touches a private attribute such as `view._store`, or uses the builtins that get around both (`getattr`, `__import__`, `importlib`, `eval` and friends).
3. **Noninterference.** For every tool, each tenant, and a sweep of arguments generated from the tool's type hints (every id, name and memo in the fixtures from both tenants, plus misses), the call runs twice: against the full store and against a copy holding only the caller's tenant. The results must be identical, and no other tenant's rows may change.

The third check doesn't care how a tool is written. It fails a list that lost its filter, a total that sums every tenant's rows, an error that says "not yours" where a missing id says "not found", a write that lands on another tenant's row, and a regression in `TenantView` itself, which shows up on every tool that reads through it. A parameter type the sweep can't generate values for fails the test, so a tool that takes a `dict` doesn't skip the check.

What it doesn't guarantee:

- **Data the tool wasn't handed.** The sweep only varies the store it passes in. A tool that reads a module-level copy of the data passes, because both runs see the same leak. The import lint catches the obvious version of this, and `test_contract_catches_breaks.py` pins the blind spot so nobody mistakes it for coverage.
- **Arguments outside the sweep.** A leak that needs an input the sweep never generated gets through.
- **Helper modules.** The lint reads the module that defines each tool, not the modules it imports.
- **The right scope.** The contract checks that a scope is declared, not that it is the correct one. A write tool declared as `invoices:read` passes; that is for review.
- **Users inside a tenant.** It checks isolation between tenants, not permissions between users of one tenant.
- **A determined author.** The lint catches honest mistakes. It is not a sandbox.
- **Numbers at run time.** The result check withholds records owned by another tenant. It can't see a total computed from them; only the noninterference test catches that, in CI.

In production the database should hold the same line as well, with row-level security keyed on the tenant, so a raw query can't cross it either.

### Seeing it fail

The `demo/broken-tool` branch adds two tools that read as reasonable in review: a dashboard total that reads `view._store`, and a `health` tool registered straight on the MCP server. On that branch the server refuses to start:

```
scoped_mcp.server.UndeclaredTool: refusing to start: tools without a scope declaration: health
```

and the contract step fails three ways:

```
FAILED test_every_tool_on_the_server_declares_its_scopes
  health: undeclared: on the MCP server without a scope declaration from the registry
FAILED test_no_tool_module_reaches_past_the_tenant_view
  scoped_mcp.tools.invoices: back door: line 49: touches private attribute ._store
FAILED test_no_tool_reads_or_writes_another_tenants_data
  paid_this_year: reads another tenant: as quillmoor with {}: ok {'tenant': 'quillmoor', 'status': 'paid', 'count': 1, 'total_cents': 260000} alone,
    ok {'tenant': 'quillmoor', 'status': 'paid', 'count': 3, 'total_cents': 401500} with other tenants present
```

The run-time result check would not have caught `paid_this_year`: it returns a total labelled with the right tenant, and a sum carries no foreign record.

On `main`, `tests/test_contract_catches_breaks.py` points each check at the tools in `tests/broken/tools.py` and asserts it fails them:

| Broken tool | Declared | Back doors | Noninterference | At run time |
|---|---|---|---|---|
| `health`, added with `@server.tool()` | fails | | | server won't start; call denied |
| `check_invoice`: "not yours" vs "not found" | | fails | fails | nothing; no foreign record is returned |
| `recent_invoices`: sorts the raw rows | | fails | fails | result withheld |
| `void_invoice`: writes by id alone | | fails | fails (writes another tenant) | nothing |
| `paid_revenue`: reads a module-level store | | fails | **passes: the blind spot** | nothing |
| `invoices_matching`: takes a `dict` | | fails (`getattr`) | fails: unprobeable | |

## Evals

`evals/` holds eleven hand-written questions with expected tool calls and results, run as each case's user through the same call path as the server. Every tool output and answer is scanned for another tenant's ids and text, and CI fails on any hit. Accuracy is reported, not gated.

```
agent  keyword (baseline: rules written against the dev cases)

split      passed   95% interval
dev        7/7      65% to 100%
heldout    2/4      15% to 85%
leaks      0 of 11 cases
```

The baseline is a keyword router I wrote by reading only the dev cases, so the harness runs in CI without an API key. Point it at Claude with `uv run --extra llm python -m evals --agent claude`. Why eleven cases need a held-out split, and what the scorer does and doesn't check: [evals/README.md](evals/README.md).

## Not here

Refresh tokens, dynamic client registration, consent screens, revocation, and step-up auth (a missing scope comes back as a tool error, not an HTTP 403 `insufficient_scope` challenge). Without dynamic client registration, MCP Inspector and desktop clients can't sign in to it as-is; the demo client is pre-registered. Audit events go to a logger, not durable storage.
