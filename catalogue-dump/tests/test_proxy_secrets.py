import json
import os

from mb_ceramics_catalogue.proxy import load_profiles
from mb_ceramics_catalogue.proxy_secrets import ProfileSecretStore, generate_password


def test_password_meets_provider_rules():
    password = generate_password()
    assert len(password) == 32
    assert any(value.isupper() for value in password)
    assert any(value.islower() for value in password)
    assert any(value.isdigit() for value in password)
    assert any(value in "_~+=" for value in password)
    assert ":" not in password and "@" not in password


def test_store_replaces_complete_generations_with_private_permissions(tmp_path):
    path = tmp_path / "profiles.json"
    store = ProfileSecretStore(path)
    assert store.install("primary", username="first-user", password="Password_1234") == 1
    assert store.install("primary", username="first-user", password="Password_5678") == 2
    assert os.stat(path).st_mode & 0o077 == 0
    profile = load_profiles(path)["primary"]
    assert profile.generation == 2
    assert profile.password == "Password_5678"
    assert json.loads(path.read_text())["primary"]["username"] == "first-user"


def test_store_remove_preserves_other_profiles(tmp_path):
    path = tmp_path / "profiles.json"
    store = ProfileSecretStore(path)
    store.install("one", username="one-user", password="Password_1234")
    store.install("two", username="two-user", password="Password_1234")
    store.remove("one")
    assert set(load_profiles(path)) == {"two"}
