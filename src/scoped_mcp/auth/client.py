"""The client half of the flow, the way an MCP client does it: discover, then PKCE, then a token for this server.

1. Call the MCP endpoint with no token. The 401 names the protected resource metadata (RFC 9728).
2. The metadata names the authorization server; fetch its metadata (RFC 8414).
3. Send the user to /authorize with an S256 code challenge and `resource` set to the MCP server (RFC 8707).
4. Check `state` and `iss` on the way back (RFC 9207), then trade the code and the verifier for a token.
"""

import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx2

from scoped_mcp.auth import pkce


class SignInError(Exception):
    pass


async def sign_in(
    http: httpx2.AsyncClient,
    *,
    mcp_url: str,
    client_id: str,
    redirect_uri: str,
    login_hint: str,
    scopes: list[str],
    resource: str | None = None,
) -> str:
    challenge = await http.get(mcp_url)
    if challenge.status_code != 401:
        raise SignInError(f"expected 401 from {mcp_url}, got {challenge.status_code}")
    found = re.search(r'resource_metadata="([^"]+)"', challenge.headers.get("www-authenticate", ""))
    if not found:
        raise SignInError("401 without a resource_metadata URL")
    prm = (await http.get(found.group(1))).json()
    issuer = prm["authorization_servers"][0].rstrip("/")
    meta = (await http.get(f"{issuer}/.well-known/oauth-authorization-server")).json()

    verifier, state = pkce.new_verifier(), secrets.token_urlsafe(16)
    resource = resource or prm["resource"]
    redirect = await http.get(
        meta["authorization_endpoint"],
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": pkce.s256(verifier),
            "code_challenge_method": "S256",
            "resource": resource,
            "login_hint": login_hint,
        },
    )
    if redirect.status_code != 302:
        raise SignInError(f"authorize: {redirect.status_code} {redirect.text}")
    back = {k: v[0] for k, v in parse_qs(urlsplit(redirect.headers["location"]).query).items()}
    if back.get("state") != state or back.get("iss") != meta["issuer"]:
        raise SignInError("state or iss on the redirect doesn't match the request")
    if "error" in back:
        raise SignInError(f"authorize: {back['error']}: {back.get('error_description', '')}")

    token = await http.post(
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": back["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    if token.status_code != 200:
        raise SignInError(f"token: {token.status_code} {token.text}")
    return token.json()["access_token"]
