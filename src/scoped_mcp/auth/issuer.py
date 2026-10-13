"""A local test authorization server: OAuth 2.1 authorization code with PKCE, and nothing else.

Not for production. It has no login page: /authorize signs in whichever test
user `login_hint` names, so the flow runs on a laptop and in CI without a
browser. Everything after that is checked the way a real server should check it:

- the client and its exact redirect URI must be registered, and an unknown
  redirect URI gets an error page, never a redirect
- the code challenge method must be S256
- the requested resource must be one this issuer serves; it becomes the
  token's audience (RFC 8707), so a token for one MCP server is refused by another
- scopes are the intersection of what the client asked for and what the user holds
- codes are single use and expire after a minute
- the token endpoint recomputes the challenge from the verifier

Missing on purpose: refresh tokens, dynamic client registration, consent,
revocation, and revoking tokens when a code is replayed.
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from scoped_mcp.auth import pkce
from scoped_mcp.models import User
from scoped_mcp.registry import SCOPES


@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True)
class _PendingCode:
    client_id: str
    redirect_uri: str
    user: User
    scopes: tuple[str, ...]
    resource: str
    challenge: str
    expires_at: float


class TestIssuer:
    __test__ = False  # not a pytest test class

    def __init__(
        self,
        *,
        url: str,
        users: list[User],
        clients: list[RegisteredClient],
        resources: list[str],
        clock: Callable[[], float] = time.time,
        code_ttl: int = 60,
        token_ttl: int = 900,
    ):
        self.url = url.rstrip("/")
        self._users = {u.subject: u for u in users}
        self._clients = {c.client_id: c for c in clients}
        self._resources = set(resources)
        self._clock = clock
        self._code_ttl = code_ttl
        self._token_ttl = token_ttl
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._kid = secrets.token_hex(8)
        self._codes: dict[str, _PendingCode] = {}

    def metadata(self) -> dict:
        """RFC 8414 authorization server metadata."""
        return {
            "issuer": self.url,
            "authorization_endpoint": f"{self.url}/authorize",
            "token_endpoint": f"{self.url}/token",
            "jwks_uri": f"{self.url}/jwks.json",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": sorted(SCOPES),
            "authorization_response_iss_parameter_supported": True,
        }

    def jwks(self) -> dict:
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self._kid, "use": "sig", "alg": "ES256"}]}

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/.well-known/oauth-authorization-server", lambda _: JSONResponse(self.metadata())),
                Route("/jwks.json", lambda _: JSONResponse(self.jwks())),
                Route("/authorize", self._authorize, methods=["GET"]),
                Route("/token", self._token, methods=["POST"]),
            ]
        )

    async def _authorize(self, request: Request) -> Response:
        q = request.query_params
        client = self._clients.get(q.get("client_id", ""))
        redirect_uri = q.get("redirect_uri", "")
        if client is None or redirect_uri not in client.redirect_uris:
            return _error("invalid_request", "unknown client_id or unregistered redirect_uri")

        def back(**params: str | None) -> RedirectResponse:
            params = {**params, "state": q.get("state"), "iss": self.url}
            return RedirectResponse(f"{redirect_uri}?{urlencode({k: v for k, v in params.items() if v})}", 302)

        if q.get("response_type") != "code":
            return back(error="unsupported_response_type")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return back(error="invalid_request", error_description="PKCE with S256 is required")
        resource = q.get("resource", "")
        if resource not in self._resources:
            return back(error="invalid_target", error_description="unknown resource")
        user = self._users.get(q.get("login_hint", ""))
        if user is None:
            return back(error="access_denied", error_description="unknown test user")
        requested = q.get("scope", "").split()
        if not requested or set(requested) - SCOPES.keys():
            return back(error="invalid_scope", error_description="request one or more known scopes")
        granted = tuple(s for s in requested if s in user.scopes)
        if not granted:
            return back(error="invalid_scope", error_description="this user holds none of the requested scopes")

        code = secrets.token_urlsafe(32)
        self._codes[code] = _PendingCode(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            user=user,
            scopes=granted,
            resource=resource,
            challenge=q["code_challenge"],
            expires_at=self._clock() + self._code_ttl,
        )
        return back(code=code)

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        if form.get("grant_type") != "authorization_code":
            return _error("unsupported_grant_type", "only authorization_code is supported")
        # pop, so a code is spent by the first attempt even when that attempt fails
        pending = self._codes.pop(str(form.get("code", "")), None)
        if pending is None or pending.expires_at < self._clock():
            return _error("invalid_grant", "code is unknown, already used or expired")
        if form.get("client_id") != pending.client_id or form.get("redirect_uri") != pending.redirect_uri:
            return _error("invalid_grant", "client_id or redirect_uri differs from the authorization request")
        if not pkce.verifier_matches(str(form.get("code_verifier", "")), pending.challenge):
            return _error("invalid_grant", "code_verifier does not match code_challenge")
        if form.get("resource") not in (None, pending.resource):
            return _error("invalid_target", "resource differs from the authorization request")

        now = int(self._clock())
        claims = {
            "iss": self.url,
            "sub": pending.user.subject,
            "aud": pending.resource,
            "iat": now,
            "exp": now + self._token_ttl,
            "jti": secrets.token_hex(16),
            "client_id": pending.client_id,
            "scope": " ".join(pending.scopes),
            "tenant": pending.user.tenant,
        }
        token = jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": self._kid, "typ": "at+jwt"})
        body = {"access_token": token, "token_type": "Bearer", "expires_in": self._token_ttl, "scope": claims["scope"]}
        return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _error(error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=400)
