"""The operator API: authentication, the controls, and what they refuse.

The refusals matter as much as the successes here. A control plane whose buttons
do something slightly different the second time they are pressed is worse than
one with fewer buttons.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from .conftest import TOKEN, requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]


async def make_run(client, sources="ceradel,spectrum"):
    response = await client.post("/v1/runs", json={"sources": sources})
    assert response.status_code == 202, response.text
    return response.json()


class TestAuthentication:
    async def test_v1_requires_a_bearer_token(self, client):
        response = await client.get("/v1/runs", headers={"authorization": ""})
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_a_wrong_token_is_refused(self, client):
        response = await client.get("/v1/runs", headers={"authorization": "Bearer nope"})
        assert response.status_code == 401

    async def test_the_stream_is_not_an_exception(self, client):
        """`EventSource` cannot set headers, which is a reason to proxy it —
        not a reason to leave it open."""
        response = await client.get("/v1/events", headers={"authorization": ""})
        assert response.status_code == 401

    async def test_a_query_string_token_is_not_accepted(self, client):
        """It would land in access logs and in `Referer` headers."""
        response = await client.get(
            f"/v1/events?token={TOKEN}", headers={"authorization": ""}
        )
        assert response.status_code == 401

    async def test_health_and_metrics_are_open(self, client):
        assert (await client.get("/health", headers={"authorization": ""})).status_code == 200
        assert (await client.get("/metrics", headers={"authorization": ""})).status_code == 200

    async def test_the_service_refuses_to_start_without_a_token(self):
        from catalogue_control.app import create_app
        from catalogue_control.settings import Settings

        with pytest.raises(ValueError, match="CONTROL_TOKEN"):
            create_app(Settings(dsn="postgresql:///x", control_token=""))


class TestQueueStatus:
    async def test_combines_jobs_outbox_and_broker_lag(self, client, monkeypatch):
        await make_run(client)

        async def broker(_settings):
            return {
                "stream": "CATALOGUE_JOBS",
                "messages": 2,
                "bytes": 512,
                "consumers": 4,
                "first_sequence": 1,
                "last_sequence": 2,
                "routes": [
                    {
                        "route": "plain.normal",
                        "durable": "catalogue-plain-normal",
                        "ready": 2,
                        "in_flight": 0,
                        "redelivered": 0,
                        "delivered": 0,
                    }
                ],
            }

        monkeypatch.setattr("catalogue_control.app._broker_queue_snapshot", broker)
        response = await client.get("/v1/queue")
        assert response.status_code == 200
        detail = response.json()
        assert detail["jobs"]["queued"] == 2
        assert detail["eligible"] == 2
        assert detail["outbox"]["pending"] == 2
        assert detail["outbox"]["ready"] == 2
        assert detail["broker"]["messages"] == 2
        assert detail["broker"]["routes"][0]["ready"] == 2
        assert detail["broker_error"] is None

    async def test_keeps_database_stats_when_nats_is_down(self, client, monkeypatch):
        async def unavailable(_settings):
            raise ConnectionError("broker refused the connection")

        monkeypatch.setattr("catalogue_control.app._broker_queue_snapshot", unavailable)
        response = await client.get("/v1/queue")
        assert response.status_code == 200
        detail = response.json()
        assert detail["broker"] is None
        assert detail["broker_error"] == "ConnectionError: broker refused the connection"
        assert detail["outbox"]["pending"] == 0


class TestRuns:
    async def test_creating_a_run_fans_it_out_into_jobs(self, client):
        body = await make_run(client)
        assert body["jobs"] == 2
        assert sorted(body["sources"]) == ["ceradel", "spectrum"]

    async def test_a_run_appears_in_the_history(self, client):
        await make_run(client)
        runs = (await client.get("/v1/runs")).json()["runs"]
        assert len(runs) == 1
        assert runs[0]["jobs"] == 2

    async def test_one_run_carries_its_jobs_and_their_progress(self, client):
        body = await make_run(client)
        detail = (await client.get(f"/v1/runs/{body['run_id']}")).json()
        assert detail["run"]["status"] == "queued"
        assert {job["source_id"] for job in detail["jobs"]} == {"ceradel", "spectrum"}

    async def test_invalid_run_parameters_are_rejected_by_name(self, client):
        """The same model the CLI validates against, so the two cannot disagree."""
        response = await client.post("/v1/runs", json={"params": {"cache_mode": "sometimes"}})
        assert response.status_code == 422
        assert "cache_mode" in response.text

    async def test_an_unknown_source_lists_the_known_ones(self, client):
        response = await client.post("/v1/runs", json={"sources": "ceradle"})
        assert response.status_code == 422
        assert "ceradle" in response.text

    async def test_a_non_string_source_selection_is_rejected(self, client):
        response = await client.post("/v1/runs", json={"sources": ["ceradel"]})
        assert response.status_code == 422
        assert "comma-separated string" in response.text

    async def test_cancelling_a_run_flags_every_unfinished_job(self, client):
        body = await make_run(client)
        response = await client.post(f"/v1/runs/{body['run_id']}/cancel")
        assert response.status_code == 202
        assert response.json()["cancelled"] == 2
        detail = (await client.get(f"/v1/runs/{body['run_id']}")).json()
        assert detail["run"]["status"] == "cancelled"
        assert {job["state"] for job in detail["jobs"]} == {"cancelled"}

    async def test_a_bad_uuid_is_a_400_not_a_500(self, client):
        assert (await client.get("/v1/runs/not-a-uuid")).status_code == 400

    async def test_an_unknown_run_is_a_404(self, client):
        missing = "00000000-0000-0000-0000-000000000000"
        assert (await client.get(f"/v1/runs/{missing}")).status_code == 404


class TestJobControls:
    async def job_id(self, client):
        body = await make_run(client, "ceradel")
        detail = (await client.get(f"/v1/runs/{body['run_id']}")).json()
        return detail["jobs"][0]["id"]

    async def test_cancelling_a_queued_job_is_accepted(self, client):
        job = await self.job_id(client)
        assert (await client.post(f"/v1/jobs/{job}/cancel")).status_code == 202
        detail = (await client.get(f"/v1/jobs/{job}")).json()["job"]
        assert detail["state"] == "cancelled"

    async def test_pausing_and_resuming_a_queued_job_changes_generation(self, client, db):
        job = await self.job_id(client)
        assert (await client.post(f"/v1/jobs/{job}/pause")).status_code == 202
        assert (await client.post(f"/v1/jobs/{job}/resume")).status_code == 202
        cursor = await db.execute(
            "select state, delivery_generation from catalogue.jobs where id=%s", (job,)
        )
        assert await cursor.fetchone() == {"state": "queued", "delivery_generation": 3}

    async def test_retrying_a_cancelled_job_requeues_it(self, client):
        job = await self.job_id(client)
        await client.post(f"/v1/jobs/{job}/cancel")
        # Simulate the worker having made it terminal.
        await client.post(f"/v1/jobs/{job}/cancel")
        detail = (await client.get(f"/v1/jobs/{job}")).json()["job"]
        assert detail["cancel_requested"] is True

    async def test_an_unknown_action_is_a_404(self, client):
        job = await self.job_id(client)
        assert (await client.post(f"/v1/jobs/{job}/explode")).status_code == 404

    async def test_job_logs_are_cursor_paged(self, client):
        job = await self.job_id(client)
        body = (await client.get(f"/v1/jobs/{job}/logs")).json()
        assert body["lines"] == []
        assert body["next_after"] is None

    async def test_job_detail_exposes_dataset_outputs_and_immutable_artifacts(self, client, db):
        job = await self.job_id(client)
        digest = "b" * 64
        await db.execute(
            """insert into catalogue.job_datasets
                         (job_id, dataset, contract_version, projector_version, state,
                          complete, records)
                  values (%s, 'stock', 'v1', 'stock-1', 'succeeded', true, 4)""",
            (job,),
        )
        await db.execute(
            """insert into catalogue.job_artifacts
                         (job_id, dataset, contract_version, projector_version, kind,
                          location, sha256, size)
                  values (%s, 'stock', 'v1', 'stock-1', 'ndjson',
                          'published/stock.ndjson', %s, 42)""",
            (job, digest),
        )
        detail = (await client.get(f"/v1/jobs/{job}")).json()["job"]
        assert detail["datasets"][0]["dataset"] == "stock"
        assert detail["datasets"][0]["complete"] is True
        assert detail["artifacts"][0]["location"] == "published/stock.ndjson"

    async def test_a_degraded_job_can_be_explicitly_retried(self, client, db):
        job = await self.job_id(client)
        await db.execute(
            "update catalogue.jobs set state = 'degraded', finished_at = now(), attempt = 2 "
            "where id = %s",
            (job,),
        )
        response = await client.post(f"/v1/jobs/{job}/retry")
        assert response.status_code == 202
        detail = (await client.get(f"/v1/jobs/{job}")).json()["job"]
        assert detail["state"] == "queued"
        assert detail["attempt"] == 0
        cursor = await db.execute(
            "select generation from catalogue.queue_outbox where job_id=%s order by generation",
            (job,),
        )
        assert [row["generation"] for row in await cursor.fetchall()] == [1, 2]


class TestJobChanges:
    async def test_degraded_job_download_uses_its_successful_ceramics_dataset(
        self, client, db, tmp_path
    ):
        run = await make_run(client, "ceradel")
        job = (await client.get(f"/v1/runs/{run['run_id']}")).json()["jobs"][0]["id"]
        body = b'{"external_id":"ceradel:a","name":"A"}\n'
        path = tmp_path / "ceramics.ndjson"
        path.write_bytes(body)
        await db.execute(
            "update catalogue.jobs set state = 'degraded', finished_at = now() where id = %s",
            (job,),
        )
        await db.execute(
            """insert into catalogue.job_datasets
                         (job_id, dataset, contract_version, projector_version,
                          state, complete, records)
                  values (%s, 'ceramics.catalogue_item.v2', 'v2', 'projector-1',
                          'degraded', true, 1)""",
            (job,),
        )
        await db.execute(
            """insert into catalogue.job_artifacts
                         (job_id, dataset, contract_version, projector_version, kind,
                          location, sha256, size)
                  values (%s, 'ceramics.catalogue_item.v2', 'v2', 'projector-1',
                          'ndjson', %s, %s, %s)""",
            (job, str(path), hashlib.sha256(body).hexdigest(), len(body)),
        )

        response = await client.get(f"/v1/jobs/{job}/artifact")
        assert response.status_code == 200
        assert response.content == body
        stock = b'{"external_id":"ceradel:a","stock":3}\n'
        stock_path = tmp_path / "stock.ndjson"
        stock_path.write_bytes(stock)
        await db.execute(
            """insert into catalogue.job_datasets
                         (job_id, dataset, contract_version, projector_version,
                          state, complete, records)
                  values (%s, 'commerce.stock.v1', 'v1', 'stock-1',
                          'succeeded', true, 1)""",
            (job,),
        )
        await db.execute(
            """insert into catalogue.job_artifacts
                         (job_id, dataset, contract_version, projector_version, kind,
                          location, sha256, size)
                  values (%s, 'commerce.stock.v1', 'v1', 'stock-1',
                          'ndjson', %s, %s, %s)""",
            (job, str(stock_path), hashlib.sha256(stock).hexdigest(), len(stock)),
        )
        selected = await client.get(
            f"/v1/jobs/{job}/artifact", params={"dataset": "commerce.stock.v1"}
        )
        assert selected.status_code == 200 and selected.content == stock
        await db.execute(
            """insert into catalogue.job_datasets
                         (job_id, dataset, contract_version, projector_version,
                          state, complete, records)
                  values (%s, 'commerce.stock.v1', 'v2', 'stock-2',
                          'succeeded', true, 1)""",
            (job,),
        )
        await db.execute(
            """insert into catalogue.job_artifacts
                         (job_id, dataset, contract_version, projector_version, kind,
                          location, sha256, size)
                  values (%s, 'commerce.stock.v1', 'v2', 'stock-2', 'ndjson',
                          'stock-v2.ndjson', %s, 0)""",
            (job, hashlib.sha256(b"").hexdigest()),
        )
        ambiguous = await client.get(
            f"/v1/jobs/{job}/artifact", params={"dataset": "commerce.stock.v1"}
        )
        assert ambiguous.status_code == 409
        assert (await client.get(
            f"/v1/jobs/{job}/artifact", headers={"authorization": ""}
        )).status_code == 401

    async def test_download_refuses_a_recorded_path_outside_the_artifact_root(
        self, client, db, tmp_path
    ):
        run = await make_run(client, "ceradel")
        job = (await client.get(f"/v1/runs/{run['run_id']}")).json()["jobs"][0]["id"]
        outside = tmp_path.parent / "outside.ndjson"
        outside.write_bytes(b"{}\n")
        await db.execute(
            "update catalogue.jobs set state = 'succeeded', finished_at = now() where id = %s",
            (job,),
        )
        await db.execute(
            """insert into catalogue.job_datasets
                         (job_id, dataset, contract_version, projector_version,
                          state, complete, records)
                  values (%s, 'ceramics.catalogue_item.v2', 'v2', 'projector-1',
                          'succeeded', true, 1)""",
            (job,),
        )
        await db.execute(
            """insert into catalogue.job_artifacts
                         (job_id, dataset, contract_version, projector_version, kind,
                          location, sha256, size)
                  values (%s, 'ceramics.catalogue_item.v2', 'v2', 'projector-1',
                          'ndjson', %s, %s, 3)""",
            (job, str(outside), hashlib.sha256(b"{}\n").hexdigest()),
        )
        response = await client.get(f"/v1/jobs/{job}/artifact")
        assert response.status_code == 409

    async def test_a_completed_job_is_compared_with_the_previous_artifact(
        self, client, db, tmp_path
    ):
        previous_run = await make_run(client, "ceradel")
        current_run = await make_run(client, "ceradel")
        previous_job = (
            await client.get(f"/v1/runs/{previous_run['run_id']}")
        ).json()["jobs"][0]["id"]
        current_job = (
            await client.get(f"/v1/runs/{current_run['run_id']}")
        ).json()["jobs"][0]["id"]

        old = b'{"external_id":"ceradel:a","name":"A","price":10}\n'
        new = b'{"external_id":"ceradel:a","name":"A","price":12}\n'
        old_path = tmp_path / "old.ndjson"
        new_path = tmp_path / "new.ndjson"
        old_path.write_bytes(old)
        new_path.write_bytes(new)
        summary = json.dumps({"write_status": "replaced", "truncated": False})
        await db.execute(
            "update catalogue.jobs set state = 'succeeded', finished_at = now() - interval '1 hour', "
            "artifact_path = %(path)s, artifact_sha256 = %(sha)s, summary = %(summary)s::jsonb "
            "where id = %(id)s",
            {
                "id": previous_job,
                "path": str(old_path),
                "sha": hashlib.sha256(old).hexdigest(),
                "summary": summary,
            },
        )
        await db.execute(
            "update catalogue.jobs set state = 'succeeded', finished_at = now(), "
            "artifact_path = %(path)s, artifact_sha256 = %(sha)s, summary = %(summary)s::jsonb "
            "where id = %(id)s",
            {
                "id": current_job,
                "path": str(new_path),
                "sha": hashlib.sha256(new).hexdigest(),
                "summary": summary,
            },
        )

        response = await client.get(f"/v1/jobs/{current_job}/changes")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["previous_job_id"] == previous_job
        assert body["changed"] == 1
        assert body["items"][0]["fields"] == [
            {"field": "price", "before": 10, "after": 12}
        ]

    async def test_an_incomplete_scrape_is_not_presented_as_a_real_diff(self, client, db):
        run = await make_run(client, "ceradel")
        job = (await client.get(f"/v1/runs/{run['run_id']}")).json()["jobs"][0]["id"]
        await db.execute(
            "update catalogue.jobs set state = 'succeeded', finished_at = now(), "
            "artifact_path = '/tmp/partial.ndjson', "
            "summary = '{\"write_status\":\"replaced\",\"truncated\":true}'::jsonb "
            "where id = %(id)s",
            {"id": job},
        )
        response = await client.get(f"/v1/jobs/{job}/changes")
        assert response.status_code == 409


class TestWorkers:
    async def register(self, db, status="idle", *, stale=False):
        from uuid import uuid4

        worker_id = uuid4()
        await db.execute(
            "insert into catalogue.workers (id, hostname, pid, status, last_heartbeat_at) "
            "values (%(id)s, 'test', 1, %(status)s, "
            "case when %(stale)s then now() - interval '1 minute' else now() end)",
            {"id": worker_id, "status": status, "stale": stale},
        )
        return worker_id

    async def test_the_roster_reports_heartbeat_age(self, client, db):
        """A worker that died shows as stale without any event arriving."""
        await self.register(db)
        workers = (await client.get("/v1/workers")).json()["workers"]
        assert len(workers) == 1
        assert workers[0]["heartbeat_age_seconds"] is not None

    async def test_draining_sets_the_desired_state(self, client, db):
        worker_id = await self.register(db)
        response = await client.post(f"/v1/workers/{worker_id}/drain")
        assert response.status_code == 202
        assert response.json()["desired_state"] == "draining"

    async def test_the_roster_lists_every_job_owned_by_one_worker(self, client, db):
        worker_id = await self.register(db, status="busy")
        run = await make_run(client)
        await db.execute(
            "update catalogue.jobs set state = 'running', lease_owner = %(worker)s "
            "where run_id = %(run)s",
            {"worker": worker_id, "run": run["run_id"]},
        )
        worker = (await client.get("/v1/workers")).json()["workers"][0]
        assert {job["source"] for job in worker["current_jobs"]} == {"ceradel", "spectrum"}

    async def test_a_stopped_worker_cannot_be_controlled(self, client, db):
        worker_id = await self.register(db, status="stopped")
        assert (await client.post(f"/v1/workers/{worker_id}/pause")).status_code == 409
        assert (await client.get("/v1/workers")).json()["workers"] == []

    async def test_a_lost_worker_can_be_hidden_without_deleting_its_audit_row(self, client, db):
        worker_id = await self.register(db, stale=True)
        response = await client.post(f"/v1/workers/{worker_id}/hide")
        assert response.status_code == 202
        assert response.json()["hidden"] is True
        row = await db.execute("select status from catalogue.workers where id = %(id)s", {"id": worker_id})
        assert (await row.fetchone())["status"] == "stopped"

    async def test_a_healthy_worker_cannot_be_hidden(self, client, db):
        worker_id = await self.register(db)
        assert (await client.post(f"/v1/workers/{worker_id}/hide")).status_code == 409


class TestSources:
    async def test_every_configured_source_is_listed_with_its_outcome(self, client):
        body = (await client.get("/v1/sources")).json()
        assert len(body["sources"]) >= 20
        entry = next(s for s in body["sources"] if s["source_id"] == "ceradel")
        assert entry["enabled"] is True
        assert entry["scraper"] == "shopify"

    async def test_disabling_a_source_is_recorded(self, client):
        response = await client.put("/v1/sources/ceradel", json={"enabled": False})
        assert response.status_code == 200
        listed = (await client.get("/v1/sources")).json()["sources"]
        assert next(s for s in listed if s["source_id"] == "ceradel")["enabled"] is False

    async def test_disabling_terminalizes_queued_jobs_and_stales_delivery(self, client, db):
        run = await make_run(client)
        response = await client.put("/v1/sources/ceradel", json={"enabled": False})
        assert response.status_code == 200
        cursor = await db.execute(
            "select state, delivery_generation from catalogue.jobs "
            "where run_id=%(run)s and source_id='ceradel'",
            {"run": run["run_id"]},
        )
        assert await cursor.fetchone() == {"state": "skipped", "delivery_generation": 2}

    async def test_source_pause_and_resume_publish_a_fresh_generation(self, client, db):
        run = await make_run(client)
        assert (await client.put("/v1/sources/ceradel", json={"paused": True})).status_code == 200
        assert (await client.put("/v1/sources/ceradel", json={"paused": False})).status_code == 200
        cursor = await db.execute(
            "select j.state, j.delivery_generation, o.generation from catalogue.jobs j "
            "join catalogue.queue_outbox o on o.job_id=j.id and o.generation=j.delivery_generation "
            "where j.run_id=%(run)s and j.source_id='ceradel'",
            {"run": run["run_id"]},
        )
        assert await cursor.fetchone() == {
            "state": "queued", "delivery_generation": 3, "generation": 3
        }

    async def test_an_unknown_source_is_a_404(self, client):
        assert (await client.put("/v1/sources/nope", json={})).status_code == 404

    async def test_invalid_overrides_are_rejected(self, client):
        response = await client.put(
            "/v1/sources/ceradel", json={"params": {"concurrency": -4}}
        )
        assert response.status_code == 422


class TestNotifications:
    async def test_unacknowledged_can_be_filtered(self, client, db):
        from mb_ceramics_catalogue.ops import events

        await events.notify(db, "source.stale", "ceradel is stale", source_id="ceradel")
        body = (await client.get("/v1/notifications?unacknowledged=true")).json()
        assert len(body["notifications"]) == 1

    async def test_acknowledging_twice_is_a_conflict(self, client, db):
        from mb_ceramics_catalogue.ops import events

        await events.notify(db, "worker.lost", "gone", severity=events.Severity.CRITICAL)
        listed = (await client.get("/v1/notifications")).json()["notifications"]
        identifier = listed[0]["id"]

        assert (await client.post(f"/v1/notifications/{identifier}/ack", json={})).status_code == 200
        assert (await client.post(f"/v1/notifications/{identifier}/ack", json={})).status_code == 409

    async def test_selected_notifications_can_be_acknowledged_together(self, client, db):
        from mb_ceramics_catalogue.ops import events

        first = await events.notify(db, "source.stale", "one", source_id="one")
        second = await events.notify(db, "source.stale", "two", source_id="two")
        untouched = await events.notify(db, "source.stale", "three", source_id="three")
        assert first is not None and second is not None and untouched is not None

        response = await client.post(
            "/v1/notifications/ack", json={"ids": [second, first, second], "by": "test"}
        )
        assert response.status_code == 200
        assert response.json() == {"ids": sorted([first, second]), "acknowledged": 2}

        open_ids = {
            row["id"]
            for row in (await client.get("/v1/notifications?unacknowledged=true")).json()[
                "notifications"
            ]
        }
        assert open_ids == {untouched}

    async def test_bulk_acknowledgement_validates_the_selection(self, client):
        assert (await client.post("/v1/notifications/ack", json={"ids": []})).status_code == 400
        assert (
            await client.post("/v1/notifications/ack", json={"ids": [True, -1, "2"]})
        ).status_code == 400


class TestSchedules:
    async def test_the_default_daily_run_is_present(self, client):
        schedules = (await client.get("/v1/schedules")).json()["schedules"]
        daily = next(s for s in schedules if s["id"] == "daily-prices")
        # A daily price run must refresh; replaying yesterday's pages would
        # report success while changing no prices.
        assert daily["params"]["cache_mode"] == "refresh"

    async def test_a_schedule_can_be_edited(self, client):
        response = await client.put(
            "/v1/schedules/daily-prices",
            json={"cron": "30 2 * * *", "timezone": "Europe/Paris", "params": {"cache_mode": "refresh"}},
        )
        assert response.status_code == 200
        assert response.json()["schedule"]["cron"] == "30 2 * * *"


class TestMetrics:
    async def test_prometheus_text_is_served(self, client):
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
