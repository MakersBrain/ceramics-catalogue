from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from mb_ceramics_catalogue.config.settings import Settings
from mb_ceramics_catalogue.ops.providers.factory import consumer, publisher
from mb_ceramics_catalogue.ops.providers.nats import NatsConsumer, NatsPublisher


def test_visibility_must_cover_the_complete_delivery_lifetime() -> None:
    with pytest.raises(ValidationError, match="complete delivery-lifetime"):
        Settings(queue_visibility_seconds=3900)
    assert Settings(queue_visibility_seconds=3901).queue_visibility_seconds == 3901


def test_cloudflare_selection_requires_every_route_and_recovery_queue() -> None:
    with pytest.raises(ValidationError, match="CATALOGUE_CF_ACCOUNT_ID"):
        Settings(queue_provider="cloudflare")


def test_role_scoped_nats_clients_do_not_provision(tmp_path) -> None:
    publish_token = tmp_path / "publish-token"
    consume_token = tmp_path / "consume-token"
    publish_token.write_text("publish-only", encoding="utf-8")
    consume_token.write_text("consume-only", encoding="utf-8")
    settings = SimpleNamespace(
        queue_provider="nats",
        nats_url="nats://queue:4222",
        nats_token="",
        nats_publish_token_file=publish_token,
        nats_consume_token_file=consume_token,
        nats_stream="CATALOGUE_JOBS",
    )

    scoped_publisher = publisher(settings)
    scoped_consumer = consumer(settings)
    assert isinstance(scoped_publisher, NatsPublisher)
    assert isinstance(scoped_consumer, NatsConsumer)
    assert scoped_publisher.queue.provision_on_connect is False
    assert scoped_consumer.queue.provision_on_connect is False


def test_legacy_shared_nats_token_keeps_implicit_provisioning() -> None:
    settings = SimpleNamespace(
        queue_provider="nats",
        nats_url="nats://queue:4222",
        nats_token="shared-admin-token",
        nats_publish_token_file=None,
        nats_consume_token_file=None,
        nats_stream="CATALOGUE_JOBS",
    )

    legacy_publisher = publisher(settings)
    legacy_consumer = consumer(settings)
    assert isinstance(legacy_publisher, NatsPublisher)
    assert isinstance(legacy_consumer, NatsConsumer)
    assert legacy_publisher.queue.provision_on_connect is True
    assert legacy_consumer.queue.provision_on_connect is True
