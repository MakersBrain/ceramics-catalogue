"""Proxy control-plane authorization and mutation tests against PostgreSQL."""

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mb_ceramics_catalogue.providers.base import Subscription, SubUser, UsageBucket, UsageReport

from catalogue_control.app import create_app
from catalogue_control.settings import Settings

from .conftest import TOKEN, postgres_dsn, requires_postgres


class FakeProvider:
    def __init__(self) -> None:
        self.created = 0

    async def create_subuser(self, **values):
        self.created += 1
        return SubUser(
            id=f"sub-{self.created}", username=values["username"], status="active",
            traffic_limit_bytes=values["traffic_limit_bytes"], auto_disable=True,
        )

    async def subscription(self):
        now = datetime.now(UTC)
        return Subscription(
            service_type="residential_proxies", traffic_limit_bytes=3_000_000_000,
            raw_traffic_limit=3, valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=29), users_limit=5,
        )

    async def usage(self, start, end, *, group_by="day"):
        key = start.isoformat() if group_by == "day" else "example.test"
        return UsageReport(
            total_transmitted_bytes=100, total_received_bytes=900, total_bytes=1000,
            requests=2,
            buckets=[UsageBucket(
                key=key, transmitted_bytes=100, received_bytes=900,
                total_bytes=1000, requests=2,
            )],
        )


def assertion(
    private_key, method: str, path: str, *, nonce=None, role="admin", auth_time=None,
):
    now = int(time.time())
    claims = {
        "kid": "test", "sub": "operator@example.test", "role": role,
        "aud": "catalogue-control", "iat": now, "exp": now + 45,
        "nonce": str(nonce or uuid4()), "method": method, "path": path,
        "auth_time": now if auth_time is None else auth_time,
    }
    raw = json.dumps(claims, separators=(",", ":")).encode()
    return {
        "x-catalogue-actor": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
        "x-catalogue-actor-signature": base64.urlsafe_b64encode(private_key.sign(raw)).rstrip(b"=").decode(),
    }


@pytest.fixture
async def proxy_client(db, tmp_path):
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    public_file = tmp_path / "operator-public.json"
    public_file.write_text(json.dumps({"test": public_pem}))
    secret_file = tmp_path / "profiles.json"
    fake = FakeProvider()
    settings = Settings(
        dsn=postgres_dsn() or "", control_token=TOKEN,
        proxy_actor_public_keys_file=public_file, proxy_secret_file=secret_file,
        proxy_mutations_enabled=True, proxy_enabled=False,
    )
    app = create_app(settings, proxy_provider=fake)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control",
        headers={"authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client, private, fake, secret_file


@pytest.mark.postgres
@requires_postgres
async def test_proxy_reads_require_a_verified_operator(proxy_client):
    client, private, _, _ = proxy_client
    assert (await client.get("/v1/proxy/overview")).status_code == 403
    viewer = assertion(private, "GET", "/v1/proxy/overview", role="viewer")
    assert (await client.get("/v1/proxy/overview", headers=viewer)).status_code == 200

    reservations_path = "/v1/proxy/reservations"
    reservations_viewer = assertion(private, "GET", reservations_path, role="viewer")
    reservations = await client.get(reservations_path, headers=reservations_viewer)
    assert reservations.status_code == 200
    assert reservations.json() == {"reservations": []}

    wrong_path = assertion(private, "GET", "/v1/proxy/cycles", role="viewer")
    assert (await client.get("/v1/proxy/overview", headers=wrong_path)).status_code == 403
    forged = dict(viewer)
    forged["x-catalogue-actor-signature"] = "AAAA"
    assert (await client.get("/v1/proxy/overview", headers=forged)).status_code == 403


@pytest.mark.postgres
@requires_postgres
async def test_provider_write_requires_recent_authentication(proxy_client):
    client, private, _, _ = proxy_client
    path = "/v1/proxy/profiles"
    headers = {
        **assertion(private, "POST", path, auth_time=int(time.time()) - 601),
        "idempotency-key": "expired-auth",
    }
    assert (await client.post(path, json={}, headers=headers)).status_code == 403


@pytest.mark.postgres
@requires_postgres
async def test_mutation_nonce_is_single_use_and_idempotency_replays(proxy_client, db):
    client, private, _, _ = proxy_client
    now = datetime.now(UTC)
    await db.execute(
        """insert into catalogue.proxy_budget_cycles
             (provider, cycle_start, cycle_end, purchased_bytes, operational_bytes,
              daily_bytes, pilot_bytes, lifecycle, reconciliation_ok, reconciled_at)
             values ('decodo', %(start)s, %(end)s, 3000000000, 2400000000,
                     80000000, 300000000, 'active', true, now())""",
        {"start": now - timedelta(days=1), "end": now + timedelta(days=29)},
    )
    path = "/v1/proxy/kill-switch/activate"
    nonce = uuid4()
    headers = {**assertion(private, "POST", path, nonce=nonce), "idempotency-key": "stop-once"}
    first = await client.post(path, json={}, headers=headers)
    assert first.status_code == 202
    assert (await client.post(path, json={}, headers=headers)).status_code == 403

    replay_headers = {**assertion(private, "POST", path), "idempotency-key": "stop-once"}
    replay = await client.post(path, json={}, headers=replay_headers)
    assert replay.status_code == 202
    assert replay.json() == first.json()


@pytest.mark.postgres
@requires_postgres
async def test_profile_creation_is_bounded_and_installs_dynamic_secret(proxy_client, db):
    client, private, fake, secret_file = proxy_client
    now = datetime.now(UTC)
    await db.execute(
        """insert into catalogue.proxy_budget_cycles
             (provider, cycle_start, cycle_end, purchased_bytes, operational_bytes,
              daily_bytes, pilot_bytes, lifecycle)
             values ('decodo', %(start)s, %(end)s, 3000000000, 2400000000,
                     80000000, 300000000, 'active')""",
        {"start": now - timedelta(days=1), "end": now + timedelta(days=29)},
    )
    path = "/v1/proxy/profiles"
    response = await client.post(path, json={
        "logical_name": "primary", "display_name": "Primary",
        "allocated_bytes": 100_000_000, "provider_traffic_limit_bytes": 90_000_000,
        "confirmation": "CREATE primary",
    }, headers={**assertion(private, "POST", path), "idempotency-key": "create-primary"})
    assert response.status_code == 201
    assert fake.created == 1
    installed = json.loads(secret_file.read_text())["primary"]
    assert installed["generation"] == 1
    assert "password" not in response.text and installed["password"] not in response.text


@pytest.mark.postgres
@requires_postgres
async def test_proxy_audit_is_immutable_for_runtime_connection(proxy_client, db):
    client, private, _, _ = proxy_client
    path = "/v1/proxy/kill-switch/activate"
    await client.post(
        path, json={},
        headers={**assertion(private, "POST", path), "idempotency-key": "audit-row"},
    )
    with pytest.raises(Exception, match="proxy audit rows are immutable"):
        await db.execute("delete from catalogue.proxy_admin_audit")


@pytest.mark.postgres
@requires_postgres
async def test_reconciliation_persists_supported_provider_groupings(proxy_client, db):
    client, private, _, _ = proxy_client
    now = datetime.now(UTC)
    await db.execute(
        """insert into catalogue.proxy_budget_cycles
             (provider, cycle_start, cycle_end, purchased_bytes, operational_bytes,
              daily_bytes, pilot_bytes, lifecycle)
             values ('decodo', %(start)s, %(end)s, 3000000000, 2400000000,
                     80000000, 300000000, 'active')""",
        {"start": now - timedelta(days=1), "end": now + timedelta(days=29)},
    )
    path = "/v1/proxy/reconcile"
    response = await client.post(
        path, json={},
        headers={**assertion(private, "POST", path), "idempotency-key": "reconcile-groups"},
    )
    assert response.status_code == 202
    cursor = await db.execute(
        """select grouping_dimension, total_bytes
             from catalogue.proxy_provider_snapshots order by grouping_dimension"""
    )
    assert [(row["grouping_dimension"], row["total_bytes"]) for row in await cursor.fetchall()] == [
        ("day", 1000), ("target", 1000),
    ]
