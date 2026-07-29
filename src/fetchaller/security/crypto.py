"""Cryptographic utilities for OAuth and authentication."""

import hashlib
import hmac
import secrets
import time
from base64 import urlsafe_b64encode
from typing import Any

import jwt


def generate_id(length: int = 32) -> str:
    """Generate a secure random URL-safe string."""
    return secrets.token_urlsafe(length)


def hash_api_key(api_key: str) -> str:
    """Hash API key for storage (avoid storing raw key in memory)."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def timing_safe_compare(a: str | bytes, b: str | bytes) -> bool:
    """
    Timing-safe comparison to prevent timing attacks.

    Uses HMAC comparison which is constant-time regardless of input.
    """
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()

    # Use HMAC for constant-time comparison
    return hmac.compare_digest(a, b)


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """
    Verify PKCE code_challenge against code_verifier (S256 only).

    Uses timing-safe comparison to prevent timing attacks.
    """
    # Compute S256 hash of verifier
    digest = hashlib.sha256(code_verifier.encode()).digest()
    # Base64url encode without padding
    computed = urlsafe_b64encode(digest).rstrip(b"=").decode()

    return timing_safe_compare(computed, code_challenge)


def create_access_token(
    client_id: str,
    api_key_hash: str,
    secret: bytes,
    ttl_seconds: int,
    *,
    audience: str,
    scope: str,
) -> str:
    """
    Create a JWT access token.

    Stores hash of API key instead of raw key for security.
    """
    now = int(time.time())
    payload = {
        "sub": client_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "aud": audience,
        "scope": scope,
        "api_key_hash": api_key_hash,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_access_token(
    token: str,
    secret: bytes,
    *,
    audience: str,
    required_scope: str,
) -> dict[str, Any] | None:
    """
    Verify a JWT access token and return its payload.

    Returns None if token is invalid or expired.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=audience,
            options={
                "require": [
                    "sub",
                    "exp",
                    "iat",
                    "aud",
                    "scope",
                    "api_key_hash",
                ]
            },
        )
        scopes = payload.get("scope")
        if not isinstance(scopes, str) or required_scope not in scopes.split():
            return None
        return payload
    except jwt.PyJWTError:
        return None
