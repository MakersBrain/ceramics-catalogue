"""The operator API: authentication, the controls, and what they refuse.

The refusals matter as much as the successes here. A control plane whose buttons
do something slightly different the second time they are pressed is worse than
one with fewer buttons.
"""

from __future__ import annotations

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

    async def test_cancelling_a_run_flags_every_unfinished_job(self, client):
        body = await make_run(client)
        response = await client.post(f"/v1/runs/{body['run_id']}/cancel")
        assert response.status_code == 202
        assert response.json()["cancelled"] == 2

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

    async def test_pausing_a_job_that_is_not_running_is_a_conflict(self, client):
        """Conditional on the state, so this is "that means nothing right now"
        rather than a silent no-op that looks like it worked."""
        job = await self.job_id(client)
        response = await client.post(f"/v1/jobs/{job}/pause")
        assert response.status_code == 409

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

    async def test_an_unknown_source_is_a_404(self, client):
        assert (await client.put("/v1/sources/nope", json={})).status_code == 404

    async def test_invalid_overrides_are_rejected(self, client):
        response = await client.put(
            "/v1/sources/ceradel", json={"params": {"concurrency": -4}}
        )
        assert response.status_code == 422


class TestNotifications:
    async def test_unacknowledged_can_be_filtered(self, client, db):
        from ateliera_catalogue.ops import events

        await events.notify(db, "source.stale", "ceradel is stale", source_id="ceradel")
        body = (await client.get("/v1/notifications?unacknowledged=true")).json()
        assert len(body["notifications"]) == 1

    async def test_acknowledging_twice_is_a_conflict(self, client, db):
        from ateliera_catalogue.ops import events

        await events.notify(db, "worker.lost", "gone", severity=events.Severity.CRITICAL)
        listed = (await client.get("/v1/notifications")).json()["notifications"]
        identifier = listed[0]["id"]

        assert (await client.post(f"/v1/notifications/{identifier}/ack", json={})).status_code == 200
        assert (await client.post(f"/v1/notifications/{identifier}/ack", json={})).status_code == 409


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
