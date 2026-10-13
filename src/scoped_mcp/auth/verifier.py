"""The resource-server side of auth: turn a bearer token into an AccessToken, or refuse it.

`JwtVerifier` implements the MCP SDK's `TokenVerifier` protocol, so any issuer
that publishes a JWKS and puts `sub`, `scope` and a `tenant` claim in its access
tokens can replace the test issuer. A token is accepted only if:

- it is signed with ES256 by a key in the issuer's JWKS (the algorithm is
  pinned, so `none` and HMAC tokens are refused)
- `iss` is the configured issuer and `aud` is this server; a token minted for
  another resource is refused, which is what stops token passthrough
- it has not expired, allowing 30 seconds of clock skew
- it carries every claim the call path needs
"""

import logging
import time
from collections.abc import Awaitable, Callable

import httpx2
import jwt
from mcp.server.auth.provider import AccessToken

log = logging.getLogger("scoped_mcp.auth")

JwksSource = Callable[[], Awaitable[dict]]

REQUIRED_CLAIMS = ["iss", "sub", "aud", "exp", "iat", "scope", "tenant", "client_id"]


class JwtVerifier:
    def __init__(self, *, issuer: str, audience: str, jwks: JwksSource):
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = await self._key(kid)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["ES256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=30,
                options={"require": REQUIRED_CLAIMS},
            )
        except (jwt.PyJWTError, LookupError) as e:
            log.info("rejected bearer token: %s", e)
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scope"].split(),
            expires_at=claims["exp"],
            resource=self._audience,
            subject=claims["sub"],
            claims={"iss": claims["iss"], "tenant": claims["tenant"]},
        )

    async def _key(self, kid: str | None) -> jwt.PyJWK:
        for jwk in (await self._jwks()).get("keys", []):
            if jwk.get("kid") == kid:
                return jwt.PyJWK(jwk, algorithm="ES256")
        raise LookupError(f"no key with kid {kid!r}")


def static_jwks(jwks: dict) -> JwksSource:
    async def get() -> dict:
        return jwks

    return get


def remote_jwks(url: str, ttl: float = 300) -> JwksSource:
    cached: tuple[float, dict] | None = None

    async def get() -> dict:
        nonlocal cached
        if cached is None or time.monotonic() - cached[0] > ttl:
            async with httpx2.AsyncClient() as http:
                response = await http.get(url)
                response.raise_for_status()
                cached = (time.monotonic(), response.json())
        return cached[1]

    return get
