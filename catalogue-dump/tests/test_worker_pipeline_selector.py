import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from mb_ceramics_catalogue import scrapers
from mb_ceramics_catalogue.config.settings import Settings
from mb_ceramics_catalogue.config.sources import SourcesFile
from mb_ceramics_catalogue.ops.queue import ClaimedJob
from mb_ceramics_catalogue.ops.worker import Worker, _legacy_terminal_state
from mb_ceramics_catalogue.pipeline.runner import PipelineResult


class Pool:
    @asynccontextmanager
    async def connection(self):
        yield object()


def test_legacy_database_rejections_are_terminally_degraded() -> None:
    assert _legacy_terminal_state(None, 1) == "degraded"
    assert _legacy_terminal_state(None, 0) == "succeeded"
    assert _legacy_terminal_state("crawl failed", 1) == "failed"


def job(pipeline: str) -> ClaimedJob:
    return ClaimedJob(
        id=uuid4(),
        run_id=uuid4(),
        source_id="shop",
        host="shop.test",
        attempt=1,
        max_attempts=3,
        requires=[],
        requires_any=[],
        params={"pipeline": pipeline},
        proxy_snapshot={},
        delivery_generation=1,
        execution_token=uuid4(),
    )


@pytest.mark.asyncio
async def test_connector_pipeline_runs_only_for_an_explicit_canary(monkeypatch, tmp_path):
    sources = SourcesFile.model_validate(
        {"shop": {"label": "Shop", "url": "https://shop.test/", "scraper": "shopify"}}
    )
    worker = Worker(Pool(), sources, Settings(dumps_dir=tmp_path))
    selected: list[str] = []

    async def canary(claimed, params, config):
        selected.append(params.pipeline)

    monkeypatch.setattr(worker, "_crawl_connector_canary", canary)
    claimed = job("connector_canary")
    worker._cancels[claimed.id] = asyncio.Event()

    await worker._crawl_and_load(claimed)

    assert selected == ["connector_canary"]


def test_legacy_remains_the_default_pipeline():
    from mb_ceramics_catalogue.config.settings import CrawlParams

    assert CrawlParams().pipeline == "legacy"
    assert CrawlParams().datasets == ("ceramics",)


def test_limited_connector_outcome_can_never_authorize_retirement():
    from mb_ceramics_catalogue.ops.worker import _connector_load_is_whole

    limited = PipelineResult(pages=1, terminal=True, enumeration_intact=False, datasets={})
    assert not _connector_load_is_whole(limited)


def test_canary_adapters_and_price_refresh_are_capability_driven():
    assert scrapers.adapter_capabilities("woocommerce").canary_adapter == (
        "woocommerce_connector"
    )
    assert scrapers.adapter_capabilities("bigcommerce_connector").price_refresh
    assert scrapers.adapter_capabilities("wix").canary_adapter == "wix_connector"
    assert scrapers.adapter_capabilities("pagecrawl").canary_adapter == "pagecrawl_connector"


def test_dataset_selection_is_explicit_validated_and_ordered():
    from pydantic import ValidationError

    from mb_ceramics_catalogue.config.settings import CrawlParams

    params = CrawlParams.model_validate({
        "pipeline": "connector_canary",
        "datasets": ["commerce.stock_observation.v1", "commerce.price_observation.v1"]
    })
    assert params.datasets == (
        "commerce.stock_observation.v1", "commerce.price_observation.v1"
    )
    with pytest.raises(ValidationError, match="duplicates"):
        CrawlParams.model_validate({"datasets": ["ceramics", "ceramics"]})
    with pytest.raises(ValidationError):
        CrawlParams.model_validate({"datasets": ["unknown.v1"]})
