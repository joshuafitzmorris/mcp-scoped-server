"""The test issuer's PKCE flow, and the verifier that guards the MCP server."""

import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
import pytest

from scoped_mcp.auth import pkce
from scoped_mcp.auth.issuer import RegisteredClient, TestIssuer
from scoped_mcp.auth.verifier import JwtVerifier, static_jwks
from scoped_mcp.store import Store

ISSUER = "http://issuer.test"
RESOURCE = "http://mcp.test/mcp"
OTHER = "http://other.test/mcp"
CLIENT = RegisteredClient("demo-client", ("http://127.0.0.1:8976/callback",))
REDIRECT = CLIENT.redirect_uris[0]

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def issuer(clock: Clock) -> TestIssuer:
    return TestIssuer(url=ISSUER, users=Store.from_fixtures().users, clients=[CLIENT], resources=[RESOURCE, OTHER], clock=clock)


@pytest.fixture
async def http(issuer: TestIssuer):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=issuer.app()), base_url=ISSUER) as client:
        yield client


def verifier_for(issuer: TestIssuer, audience: str = RESOURCE) -> JwtVerifier:
    return JwtVerifier(issuer=ISSUER, audience=audience, jwks=static_jwks(issuer.jwks()))


async def authorize(http: httpx2.AsyncClient, verifier: str, **overrides: str) -> httpx2.Response:
    params = {
        "response_type": "code",
        "client_id": CLIENT.client_id,
        "redirect_uri": REDIRECT,
        "scope": "invoices:read contracts:read",
        "state": "xyz",
        "code_challenge": pkce.s256(verifier),
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "login_hint": "ana@quillmoor.example",
    }
    return await http.get("/authorize", params={**params, **overrides})


def redirect_params(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 302, response.text
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


async def exchange(http: httpx2.AsyncClient, code: str, verifier: str, **overrides: str) -> httpx2.Response:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": CLIENT.client_id,
        "code_verifier": verifier,
        **overrides,
    }
    return await http.post("/token", data=form)


async def test_the_full_flow_yields_a_token_the_server_accepts(http, issuer) -> None:
    v = pkce.new_verifier()
    back = redirect_params(await authorize(http, v))
    assert back["state"] == "xyz" and back["iss"] == ISSUER
    token = (await exchange(http, back["code"], v)).json()["access_token"]

    access = await verifier_for(issuer).verify_token(token)
    assert access is not None
    assert access.subject == "ana@quillmoor.example"
    assert access.claims == {"iss": ISSUER, "tenant": "quillmoor"}
    assert sorted(access.scopes) == ["contracts:read", "invoices:read"]
    assert access.resource == RESOURCE


async def test_the_wrong_verifier_is_refused(http) -> None:
    back = redirect_params(await authorize(http, pkce.new_verifier()))
    response = await exchange(http, back["code"], pkce.new_verifier())
    assert response.status_code == 400 and "code_verifier" in response.json()["error_description"]


async def test_a_code_works_once_even_if_the_first_attempt_failed(http) -> None:
    v = pkce.new_verifier()
    code = redirect_params(await authorize(http, v))["code"]
    assert (await exchange(http, code, "x" * 43)).status_code == 400
    assert (await exchange(http, code, v)).json()["error"] == "invalid_grant"


async def test_an_expired_code_is_refused(http, clock) -> None:
    v = pkce.new_verifier()
    code = redirect_params(await authorize(http, v))["code"]
    clock.now += 61
    assert (await exchange(http, code, v)).json()["error"] == "invalid_grant"


async def test_the_plain_challenge_method_is_refused(http) -> None:
    v = pkce.new_verifier()
    back = redirect_params(await authorize(http, v, code_challenge=v, code_challenge_method="plain"))
    assert back["error"] == "invalid_request"


async def test_an_unregistered_redirect_uri_gets_an_error_page_not_a_redirect(http) -> None:
    response = await authorize(http, pkce.new_verifier(), redirect_uri="https://evil.example/callback")
    assert response.status_code == 400 and "location" not in response.headers


async def test_a_redirect_uri_that_differs_at_the_token_endpoint_is_refused(http) -> None:
    v = pkce.new_verifier()
    code = redirect_params(await authorize(http, v))["code"]
    assert (await exchange(http, code, v, redirect_uri="http://127.0.0.1:9999/callback")).status_code == 400


async def test_scopes_are_capped_at_what_the_user_holds(http, issuer) -> None:
    v = pkce.new_verifier()
    back = redirect_params(await authorize(http, v, login_hint="ben@quillmoor.example", scope="invoices:read invoices:write"))
    token = (await exchange(http, back["code"], v)).json()
    assert token["scope"] == "invoices:read"

    only_write = redirect_params(await authorize(http, v, login_hint="ben@quillmoor.example", scope="invoices:write"))
    assert only_write["error"] == "invalid_scope"


async def test_an_unknown_resource_is_refused(http) -> None:
    back = redirect_params(await authorize(http, pkce.new_verifier(), resource="http://elsewhere.test/mcp"))
    assert back["error"] == "invalid_target"


async def test_a_token_for_another_server_is_refused(http, issuer) -> None:
    v = pkce.new_verifier()
    back = redirect_params(await authorize(http, v, resource=OTHER))
    token = (await exchange(http, back["code"], v)).json()["access_token"]
    assert await verifier_for(issuer, audience=OTHER).verify_token(token) is not None
    assert await verifier_for(issuer).verify_token(token) is None


async def test_an_expired_token_is_refused(http, issuer, clock) -> None:
    v = pkce.new_verifier()
    clock.now -= 3600  # minted an hour ago with a 15 minute lifetime
    back = redirect_params(await authorize(http, v))
    token = (await exchange(http, back["code"], v)).json()["access_token"]
    assert await verifier_for(issuer).verify_token(token) is None


async def test_tokens_not_signed_by_the_issuer_are_refused(issuer) -> None:
    claims = {
        "iss": ISSUER,
        "sub": "ana@quillmoor.example",
        "aud": RESOURCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "scope": "invoices:read",
        "tenant": "tarnwick",
        "client_id": "demo-client",
    }
    kid = issuer.jwks()["keys"][0]["kid"]
    impostor = TestIssuer(url=ISSUER, users=[], clients=[], resources=[])
    forged = [
        jwt.encode(claims, "a-guessable-shared-secret-of-32-bytes", algorithm="HS256", headers={"kid": kid}),
        jwt.encode(claims, impostor._key, algorithm="ES256", headers={"kid": kid}),
        jwt.encode(claims, None, algorithm="none"),
    ]
    for token in forged:
        assert await verifier_for(issuer).verify_token(token) is None


async def test_a_token_without_a_tenant_claim_is_refused(issuer) -> None:
    claims = {"iss": ISSUER, "sub": "x", "aud": RESOURCE, "iat": int(time.time()), "exp": int(time.time()) + 600, "scope": "", "client_id": "c"}
    token = jwt.encode(claims, issuer._key, algorithm="ES256", headers={"kid": issuer.jwks()["keys"][0]["kid"]})
    assert await verifier_for(issuer).verify_token(token) is None
