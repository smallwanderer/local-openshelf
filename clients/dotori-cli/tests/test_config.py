import json

import pytest

from dotori_cli.config import (
    ConfigError,
    add_remote,
    list_accounts,
    list_profiles,
    list_remotes,
    load_context,
    load_profile,
    save_account,
    save_profile,
    set_remote_url,
    use_account,
)


def test_profile_and_credentials_are_stored_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))

    saved = save_profile("home", "https://dotori.example.com/", "dtr_cli_secret")

    assert saved.name == "home"
    assert saved.server == "https://dotori.example.com"
    assert load_profile().token == "dtr_cli_secret"
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    credentials = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert "dtr_cli_secret" not in json.dumps(config)
    assert config["remotes"]["origin"]["url"] == "https://dotori.example.com"
    assert config["accounts"]["origin"]["home"]["workspace"] is None
    assert config["accounts"]["origin"]["home"]["credentials"]["default"]["token_type"] == "cli"
    assert credentials["tokens"]["origin/home/default"] == "dtr_cli_secret"
    assert list_profiles()[0] == "home"


def test_environment_token_overrides_saved_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))
    save_profile("home", "http://127.0.0.1:8000", "dtr_cli_saved")
    monkeypatch.setenv("DOTORI_CLI_TOKEN", "dtr_cli_environment")

    assert load_profile("home").token == "dtr_cli_environment"


def test_invalid_server_or_token_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ConfigError):
        save_profile("home", "dotori.example.com", "dtr_cli_secret")
    with pytest.raises(ConfigError):
        save_profile(
            "home",
            "https://dotori.example.com",
            "legacy-token",
            token_type="cli",
        )


def test_sync_token_profile_is_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))

    saved = save_profile("folder", "https://dotori.example.com", "a" * 64)

    assert saved.token_type == "sync"
    assert list_profiles()[1][0]["token_type"] == "sync"


def test_remote_account_and_credentials_are_separate_context_levels(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))
    add_remote("origin", "https://dotori.example.com")
    identity = {
        "account": {"id": "42", "email": "user@example.com", "display_name": "User"},
        "token": {"type": "cli", "scopes": ["documents:read", "search"]},
        "workspace": None,
    }
    save_account(
        "origin",
        "personal",
        "dtr_cli_readonly",
        credential="default",
        identity=identity,
    )
    save_account(
        "origin",
        "personal",
        "a" * 64,
        token_type="sync",
        credential="sync",
        identity={**identity, "token": {"type": "sync", "scopes": ["sync"]}},
    )

    general = load_context(remote="origin", account="personal")
    sync = load_context(remote="origin", account="personal", purpose="sync")

    assert general.credential == "default"
    assert general.token == "dtr_cli_readonly"
    assert sync.credential == "sync"
    assert sync.token == "a" * 64
    assert list_remotes()[1] == [
        {"name": "origin", "url": "https://dotori.example.com", "active": True}
    ]
    account = list_accounts("origin")[1][0]
    assert account["server_account_id"] == "42"
    assert set(account["credentials"]) == {"default", "sync"}


def test_account_use_changes_active_context(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))
    add_remote("origin", "https://dotori.example.com")
    save_account("origin", "first", "dtr_cli_first")
    save_account("origin", "second", "dtr_cli_second")

    use_account("origin", "first")

    assert load_context().name == "first"


def test_remote_url_change_updates_all_accounts_without_rewriting_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))
    add_remote("origin", "https://old.example.com")
    save_account("origin", "first", "dtr_cli_first")
    save_account("origin", "second", "dtr_cli_second")

    set_remote_url("origin", "https://new.example.com")

    assert load_context(remote="origin", account="first").server == "https://new.example.com"
    assert load_context(remote="origin", account="second").server == "https://new.example.com"
    credentials = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert credentials["tokens"]["origin/first/default"] == "dtr_cli_first"
    assert credentials["tokens"]["origin/second/default"] == "dtr_cli_second"


def test_account_rejects_a_credential_for_another_verified_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))
    add_remote("origin", "https://dotori.example.com")
    save_account(
        "origin",
        "personal",
        "dtr_cli_first",
        identity={"account": {"id": "1"}, "token": {"type": "cli"}},
    )

    with pytest.raises(ConfigError, match="different server account"):
        save_account(
            "origin",
            "personal",
            "dtr_cli_other",
            credential="other",
            identity={"account": {"id": "2"}, "token": {"type": "cli"}},
        )


def test_legacy_profiles_are_migrated_without_deleting_the_source_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))
    (tmp_path / "profiles.json").write_text(
        json.dumps({
            "active_profile": "home",
            "profiles": {
                "home": {"server": "https://dotori.example.com", "token_type": "cli"},
                "folders": {"server": "https://dotori.example.com", "token_type": "sync"},
            },
        }),
        encoding="utf-8",
    )
    (tmp_path / "credentials.json").write_text(
        json.dumps({"tokens": {"home": "dtr_cli_home", "folders": "a" * 64}}),
        encoding="utf-8",
    )

    migrated = load_profile("home")

    assert migrated.remote == "origin"
    assert migrated.token == "dtr_cli_home"
    assert (tmp_path / "profiles.json").exists()
    assert json.loads((tmp_path / "credentials.v1.json").read_text(encoding="utf-8"))[
        "tokens"
    ]["home"] == "dtr_cli_home"
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert set(config["accounts"]["origin"]) == {"home", "folders"}
