"""Clerk JWT verification.

Validates Bearer tokens issued by Clerk using their JWKS endpoint. The
PyJWKClient caches the public keys so this does not hit the network on every
request.

This mirrors the auth layer in the trading_agents and report-suite services on
purpose: all of them sit behind the same Clerk application and the same user
IDs, which is what lets them share one credit wallet.
"""

from __future__ import annotations

import os
from typing import Optional

import jwt as pyjwt
from jwt import PyJWKClient

_CLERK_JWKS_URL = os.getenv("CLERK_JWKS_URL", "")

_jwk_client: Optional[PyJWKClient] = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        if not _CLERK_JWKS_URL:
            raise RuntimeError(
                "CLERK_JWKS_URL environment variable is not set. "
                "Set it to your Clerk JWKS endpoint, e.g. "
                "https://clerk.example.com/.well-known/jwks.json"
            )
        _jwk_client = PyJWKClient(_CLERK_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwk_client


def verify_clerk_token(token: str) -> dict:
    """Verify a Clerk JWT and return the decoded payload.

    Raises:
        jwt.InvalidTokenError: token invalid, expired, or badly signed.
        RuntimeError: CLERK_JWKS_URL is not configured.
    """
    client = _get_jwk_client()
    signing_key = client.get_signing_key_from_jwt(token)

    return pyjwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        options={
            "verify_exp": True,
            "verify_iat": True,
            "require": ["sub", "exp", "iat"],
        },
    )


def get_user_data_from_token(token: str) -> tuple[str, Optional[str]]:
    """Verify token and return (user_id, email)."""
    payload = verify_clerk_token(token)
    return payload["sub"], payload.get("email")
