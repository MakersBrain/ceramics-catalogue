"""The registry is the contract the control plane asks capability questions of."""

import inspect

import pytest

from mb_ceramics_catalogue.providers.base import ProviderError, ProxyProvider
from mb_ceramics_catalogue.providers.registry import REGISTRY, known, spec


def test_every_spec_names_itself_by_its_registry_key():
    """`name` is the value in the `provider` column of every cycle, profile,
    usage row and reservation. A key that disagrees with its spec would write
    rows nothing can find again."""
    for key, provider_spec in REGISTRY.items():
        assert provider_spec.name == key


def test_confirmation_text_cannot_be_shared_between_providers():
    """Typing "OPEN DECODO CYCLE" must not open an IPRoyal cycle."""
    phrases = {spec(name).confirmation("open") for name in known()}
    assert len(phrases) == len(known())
    assert spec("decodo").confirmation("open") == "OPEN DECODO CYCLE"
    assert spec("iproyal").confirmation("close") == "CLOSE IPROYAL CYCLE"


def test_lock_keys_are_per_provider():
    """The lock used to be the constant `proxy:decodo`, which serialised every
    provider's cycle mutations against each other."""
    keys = {spec(name).lock_key for name in known()}
    assert len(keys) == len(known())
    assert spec("decodo").lock_key == "proxy:decodo"


def test_an_unknown_provider_raises_rather_than_defaulting():
    with pytest.raises(ProviderError, match="no such proxy provider"):
        spec("not-a-provider")


def test_capabilities_match_what_each_api_actually_offers():
    # Decodo sells a dated subscription; IPRoyal sells a prepaid balance.
    assert spec("decodo").proposes_cycles is True
    assert spec("iproyal").proposes_cycles is False
    assert spec("webshare").proposes_cycles is True
    # Only Decodo exposes a per-sub-user status.
    assert spec("decodo").has_subuser_status is True
    assert spec("iproyal").has_subuser_status is False
    # Webshare issues its own sub-user credentials.
    assert spec("webshare").can_provision_subusers is False
    assert spec("proxyscrape").can_provision_subusers is False
    assert spec("decodo").can_provision_subusers is True


def test_a_probe_url_is_absent_rather_than_guessed():
    """A probe spends real traffic, so an unverified endpoint stays unset."""
    assert spec("decodo").probe_url == "https://ip.decodo.com/json"
    assert spec("iproyal").probe_url is None
    assert spec("webshare").probe_url is None
    assert spec("proxyscrape").probe_url is None


#: `ProxyProvider` is a plain Protocol, not @runtime_checkable, so isinstance
#: would not work -- and would only check that the names exist even if it did.
#: Asserting the methods are present *and* awaitable is the stronger check, and
#: it is what catches a new adapter that forgot one or wrote it synchronously.
PROTOCOL_METHODS = [
    name for name in vars(ProxyProvider) if not name.startswith("_")
]


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_provider_builds_and_implements_the_whole_protocol(name: str) -> None:
    built = spec(name).build("secret-api-key", base_url="https://provider.test")
    assert PROTOCOL_METHODS, "the protocol should expose methods to check"
    for method in PROTOCOL_METHODS:
        implementation = getattr(built, method, None)
        assert implementation is not None, f"{name} is missing {method}()"
        assert inspect.iscoroutinefunction(implementation), f"{name}.{method}() is not async"


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_provider_refuses_an_empty_credential(name: str) -> None:
    with pytest.raises(ProviderError, match=r"missing_api_key|is not configured"):
        spec(name).build("", base_url="https://provider.test")
