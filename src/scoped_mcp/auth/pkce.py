"""PKCE (RFC 7636), S256 only. Both sides use these, so the client and the issuer can't drift apart."""

import base64
import hashlib
import hmac
import re
import secrets

_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")


def new_verifier() -> str:
    return secrets.token_urlsafe(48)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verifier_matches(verifier: str, challenge: str) -> bool:
    return bool(_VERIFIER.fullmatch(verifier)) and hmac.compare_digest(s256(verifier), challenge)
