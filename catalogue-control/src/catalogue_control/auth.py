"""Short-lived operator assertions for sensitive proxy administration."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.requests import Request


class ActorRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    nonce: UUID
    auth_time: int | None = None


def load_public_keys(path: Path | None) -> dict[str, Ed25519PublicKey]:
    if path is None or not path.is_file() or path.stat().st_size == 0:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("actor verification key file must contain a key-id object")
    keys: dict[str, Ed25519PublicKey] = {}
    for key_id, pem in payload.items():
        if not isinstance(key_id, str) or not isinstance(pem, str):
            raise ValueError("actor verification keys must be string PEM values")
        key = serialization.load_pem_public_key(pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("actor verification key must be Ed25519")
        keys[key_id] = key
    return keys


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise ActorRejected("malformed operator assertion") from error


async def require_actor(
    request: Request, *, role: str = "viewer", recent: bool = False,
) -> Actor:
    encoded = request.headers.get("x-catalogue-actor", "")
    signature = request.headers.get("x-catalogue-actor-signature", "")
    if not encoded or not signature:
        raise ActorRejected("operator assertion is required")
    raw = _decode(encoded)
    try:
        claims: Any = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ActorRejected("malformed operator assertion") from error
    if not isinstance(claims, dict):
        raise ActorRejected("malformed operator assertion")
    key = request.app.state.actor_keys.get(str(claims.get("kid", "")))
    if key is None:
        raise ActorRejected("unknown operator assertion key")
    try:
        key.verify(_decode(signature), raw)
    except InvalidSignature as error:
        raise ActorRejected("invalid operator assertion signature") from error

    now = int(time.time())
    try:
        issued = int(claims["iat"])
        expires = int(claims["exp"])
        nonce = UUID(str(claims["nonce"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ActorRejected("operator assertion omitted required claims") from error
    if issued > now + 10 or expires < now or expires - issued > 60:
        raise ActorRejected("operator assertion is expired or has an unsafe lifetime")
    if claims.get("aud") != "catalogue-control":
        raise ActorRejected("operator assertion has the wrong audience")
    if claims.get("method") != request.method or claims.get("path") != request.url.path:
        raise ActorRejected("operator assertion does not match this request")
    actor_id = str(claims.get("sub", ""))
    actor_role = str(claims.get("role", ""))
    if not actor_id or actor_role not in {"viewer", "admin"}:
        raise ActorRejected("operator assertion has no valid identity or role")
    if role == "admin" and actor_role != "admin":
        raise ActorRejected("administrator role is required")
    auth_time = claims.get("auth_time")
    if auth_time is not None:
        try:
            auth_time = int(auth_time)
        except (TypeError, ValueError) as error:
            raise ActorRejected("operator authentication time is malformed") from error
    if recent and (auth_time is None or auth_time > now + 10 or now - auth_time > 600):
        raise ActorRejected("recent operator authentication is required")

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        async with request.app.state.pool.connection() as connection:
            cursor = await connection.execute(
                """
                insert into catalogue.proxy_actor_nonces(nonce, actor, expires_at)
                values (%(nonce)s, %(actor)s, to_timestamp(%(expires)s))
                on conflict (nonce) do nothing returning nonce
                """,
                {"nonce": nonce, "actor": actor_id, "expires": expires},
            )
            if await cursor.fetchone() is None:
                raise ActorRejected("operator assertion nonce was already used")
    request.scope["operator"] = actor_id
    return Actor(actor_id, actor_role, nonce, auth_time)
