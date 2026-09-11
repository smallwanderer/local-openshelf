from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


CONFIG_SCHEMA_VERSION = 2
DEFAULT_REMOTE = "origin"
DEFAULT_CREDENTIAL = "default"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ServerProfile:
    # ``name`` remains the account alias for compatibility with the 0.2 client.
    name: str
    server: str
    token: str
    token_type: str = "cli"
    remote: str = DEFAULT_REMOTE
    workspace: str | None = None
    credential: str = DEFAULT_CREDENTIAL


def config_directory() -> Path:
    override = os.environ.get("DOTORI_CLI_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
        return base / "Dotori"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "dotori"


def normalize_server_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("Server URL must start with http:// or https://.")
    if parsed.query or parsed.fragment:
        raise ConfigError("Server URL must not contain a query string or fragment.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _normalize_alias(value: str, label: str) -> str:
    alias = str(value or "").strip()
    if (
        not alias
        or any(char.isspace() for char in alias)
        or "/" in alias
        or "\\" in alias
        or alias in {".", ".."}
    ):
        raise ConfigError(f"{label} must be non-empty and contain no whitespace or slashes.")
    return alias


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read CLI configuration: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"CLI configuration must contain a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict, *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if secret and os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


def _empty_config() -> dict:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "active": {"remote": None, "account": None, "workspace": None},
        "remotes": {},
        "accounts": {},
    }


def _empty_credentials() -> dict:
    return {"schema_version": CONFIG_SCHEMA_VERSION, "tokens": {}}


def infer_token_type(token: str) -> str:
    return "cli" if token.startswith("dtr_cli_") else "sync"


def _validate_token(token: str, token_type: str) -> str:
    if not token or any(char.isspace() for char in token):
        raise ConfigError("Token must be non-empty and contain no whitespace.")
    resolved = infer_token_type(token) if token_type == "auto" else token_type
    if resolved not in {"cli", "sync"}:
        raise ConfigError("Token type must be 'cli' or 'sync'.")
    if resolved == "cli" and not token.startswith("dtr_cli_"):
        raise ConfigError("Expected a Dotori CLI token beginning with 'dtr_cli_'.")
    return resolved


def _unique_alias(preferred: str, existing: set[str]) -> str:
    candidate = preferred or "remote"
    if candidate not in existing:
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in existing:
        suffix += 1
    return f"{candidate}-{suffix}"


def _migrate_legacy_config(directory: Path) -> tuple[dict, dict]:
    legacy_profiles = _read_json(directory / "profiles.json")
    legacy_credentials = _read_json(directory / "credentials.json")
    profile_items = legacy_profiles.get("profiles") or {}
    if not isinstance(profile_items, dict) or not profile_items:
        return _empty_config(), _empty_credentials()

    config = _empty_config()
    credentials = _empty_credentials()
    active_profile = legacy_profiles.get("active_profile")
    active_data = profile_items.get(active_profile) if active_profile else None
    active_url = ""
    if isinstance(active_data, dict) and active_data.get("server"):
        active_url = normalize_server_url(str(active_data["server"]))

    url_to_remote: dict[str, str] = {}
    if active_url:
        url_to_remote[active_url] = DEFAULT_REMOTE
        config["remotes"][DEFAULT_REMOTE] = {"url": active_url}
        config["accounts"][DEFAULT_REMOTE] = {}

    for profile_name, data in profile_items.items():
        if not isinstance(data, dict) or not data.get("server"):
            continue
        account_alias = _normalize_alias(str(profile_name), "Legacy profile name")
        server = normalize_server_url(str(data["server"]))
        remote = url_to_remote.get(server)
        if remote is None:
            preferred = account_alias if config["remotes"] else DEFAULT_REMOTE
            remote = _unique_alias(preferred, set(config["remotes"]))
            url_to_remote[server] = remote
        config["remotes"].setdefault(remote, {"url": server})
        accounts = config["accounts"].setdefault(remote, {})
        if account_alias in accounts:
            account_alias = _unique_alias(account_alias, set(accounts))
        token_type = str(data.get("token_type") or "cli")
        accounts[account_alias] = {
            "server_account_id": "",
            "email": "",
            "display_name": "",
            "workspace": None,
            "credentials": {
                DEFAULT_CREDENTIAL: {"token_type": token_type, "scopes": []}
            },
        }
        raw_token = str((legacy_credentials.get("tokens") or {}).get(profile_name) or "")
        if raw_token:
            credentials["tokens"][f"{remote}/{account_alias}/{DEFAULT_CREDENTIAL}"] = raw_token
        if profile_name == active_profile:
            config["active"].update({"remote": remote, "account": account_alias})

    if not config["active"]["remote"] and config["remotes"]:
        first_remote = next(iter(config["remotes"]))
        first_account = next(iter(config["accounts"].get(first_remote, {})), None)
        config["active"].update({"remote": first_remote, "account": first_account})

    credentials_backup = directory / "credentials.v1.json"
    if legacy_credentials and not credentials_backup.exists():
        _write_json(credentials_backup, legacy_credentials, secret=True)
    _write_json(directory / "config.json", config)
    _write_json(directory / "credentials.json", credentials, secret=True)
    return config, credentials


def _load_state() -> tuple[dict, dict]:
    directory = config_directory()
    config_path = directory / "config.json"
    if not config_path.exists():
        return _migrate_legacy_config(directory)
    config = _read_json(config_path)
    if config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigError("Unsupported CLI configuration schema version.")
    credentials = _read_json(directory / "credentials.json") or _empty_credentials()
    if credentials.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigError("Unsupported CLI credential schema version.")
    return config, credentials


def _save_state(config: dict, credentials: dict | None = None) -> None:
    directory = config_directory()
    _write_json(directory / "config.json", config)
    if credentials is not None:
        _write_json(directory / "credentials.json", credentials, secret=True)


def add_remote(name: str, server: str, *, allow_existing=False) -> dict:
    remote = _normalize_alias(name, "Remote name")
    normalized_server = normalize_server_url(server)
    config, _ = _load_state()
    existing = (config.get("remotes") or {}).get(remote)
    if existing:
        if allow_existing and existing.get("url") == normalized_server:
            return {"name": remote, "url": normalized_server}
        raise ConfigError(f"Dotori remote already exists: {remote}")
    config.setdefault("remotes", {})[remote] = {"url": normalized_server}
    config.setdefault("accounts", {}).setdefault(remote, {})
    if not (config.get("active") or {}).get("remote"):
        config["active"]["remote"] = remote
    _save_state(config)
    return {"name": remote, "url": normalized_server}


def set_remote_url(name: str, server: str) -> dict:
    remote = _normalize_alias(name, "Remote name")
    normalized_server = normalize_server_url(server)
    config, _ = _load_state()
    if remote not in (config.get("remotes") or {}):
        raise ConfigError(f"Dotori remote does not exist: {remote}")
    config["remotes"][remote]["url"] = normalized_server
    _save_state(config)
    return {"name": remote, "url": normalized_server}


def get_remote(name: str) -> dict:
    remote = _normalize_alias(name, "Remote name")
    config, _ = _load_state()
    data = (config.get("remotes") or {}).get(remote)
    if not isinstance(data, dict) or not data.get("url"):
        raise ConfigError(f"Dotori remote does not exist: {remote}")
    return {"name": remote, "url": normalize_server_url(str(data["url"]))}


def list_remotes() -> tuple[str | None, list[dict]]:
    config, _ = _load_state()
    active = (config.get("active") or {}).get("remote")
    items = [
        {"name": name, "url": data.get("url", ""), "active": name == active}
        for name, data in sorted((config.get("remotes") or {}).items())
        if isinstance(data, dict)
    ]
    return active, items


def save_account(
    remote: str,
    account: str,
    token: str,
    *,
    token_type: str = "auto",
    credential: str = DEFAULT_CREDENTIAL,
    identity: dict | None = None,
) -> ServerProfile:
    remote_alias = _normalize_alias(remote, "Remote name")
    account_alias = _normalize_alias(account, "Account name")
    credential_alias = _normalize_alias(credential, "Credential name")
    resolved_token_type = _validate_token(token, token_type)
    config, credentials = _load_state()
    remote_data = (config.get("remotes") or {}).get(remote_alias)
    if not isinstance(remote_data, dict) or not remote_data.get("url"):
        raise ConfigError(f"Dotori remote does not exist: {remote_alias}")

    identity = identity or {}
    account_identity = identity.get("account") or {}
    token_identity = identity.get("token") or {}
    workspace = identity.get("workspace")
    account_data = config.setdefault("accounts", {}).setdefault(remote_alias, {}).setdefault(
        account_alias,
        {
            "server_account_id": "",
            "email": "",
            "display_name": "",
            "workspace": None,
            "credentials": {},
        },
    )
    if account_identity:
        existing_account_id = str(account_data.get("server_account_id") or "")
        verified_account_id = str(account_identity.get("id") or "")
        if (
            existing_account_id
            and verified_account_id
            and existing_account_id != verified_account_id
        ):
            raise ConfigError(
                f"Credential belongs to a different server account than "
                f"{remote_alias}/{account_alias}."
            )
        account_data.update({
            "server_account_id": verified_account_id,
            "email": str(account_identity.get("email") or ""),
            "display_name": str(account_identity.get("display_name") or ""),
            "workspace": workspace,
        })
    account_data.setdefault("credentials", {})[credential_alias] = {
        "token_type": str(token_identity.get("type") or resolved_token_type),
        "scopes": list(token_identity.get("scopes") or []),
    }
    credentials.setdefault("tokens", {})[
        f"{remote_alias}/{account_alias}/{credential_alias}"
    ] = token
    config["active"].update({
        "remote": remote_alias,
        "account": account_alias,
        "workspace": account_data.get("workspace"),
    })
    _save_state(config, credentials)
    return load_context(
        remote=remote_alias,
        account=account_alias,
        credential=credential_alias,
    )


def list_accounts(remote: str | None = None) -> tuple[dict, list[dict]]:
    config, _ = _load_state()
    active = config.get("active") or {}
    selected_remote = remote or active.get("remote")
    if not selected_remote:
        return active, []
    selected_remote = _normalize_alias(selected_remote, "Remote name")
    if selected_remote not in (config.get("remotes") or {}):
        raise ConfigError(f"Dotori remote does not exist: {selected_remote}")
    items = []
    for name, data in sorted((config.get("accounts") or {}).get(selected_remote, {}).items()):
        if not isinstance(data, dict):
            continue
        items.append({
            "name": name,
            "remote": selected_remote,
            "server_account_id": data.get("server_account_id", ""),
            "email": data.get("email", ""),
            "display_name": data.get("display_name", ""),
            "workspace": data.get("workspace"),
            "credentials": data.get("credentials") or {},
            "active": selected_remote == active.get("remote") and name == active.get("account"),
        })
    return active, items


def use_account(remote: str, account: str) -> dict:
    remote_alias = _normalize_alias(remote, "Remote name")
    account_alias = _normalize_alias(account, "Account name")
    config, _ = _load_state()
    account_data = ((config.get("accounts") or {}).get(remote_alias) or {}).get(account_alias)
    if not isinstance(account_data, dict):
        raise ConfigError(f"Dotori account does not exist: {remote_alias}/{account_alias}")
    config["active"].update({
        "remote": remote_alias,
        "account": account_alias,
        "workspace": account_data.get("workspace"),
    })
    _save_state(config)
    return {"remote": remote_alias, "account": account_alias, "workspace": account_data.get("workspace")}


def _resolve_legacy_profile(config: dict, name: str) -> tuple[str, str]:
    matches = [
        (remote, name)
        for remote, accounts in (config.get("accounts") or {}).items()
        if isinstance(accounts, dict) and name in accounts
    ]
    if not matches:
        raise ConfigError(f"Dotori profile does not exist: {name}")
    if len(matches) > 1:
        raise ConfigError(f"Profile name is ambiguous; pass --remote with --account: {name}")
    return matches[0]


def load_context(
    *,
    remote: str | None = None,
    account: str | None = None,
    credential: str | None = None,
    purpose: str = "default",
    legacy_profile: str | None = None,
) -> ServerProfile:
    config, credentials = _load_state()
    active = config.get("active") or {}
    if legacy_profile:
        remote_alias, account_alias = _resolve_legacy_profile(config, legacy_profile)
    else:
        remote_alias = remote or active.get("remote")
        if not remote_alias:
            raise ConfigError("No Dotori remote is configured. Run 'dotori remote add' first.")
        remote_alias = _normalize_alias(remote_alias, "Remote name")
        account_alias = account
        if not account_alias and remote_alias == active.get("remote"):
            account_alias = active.get("account")
        accounts_for_remote = (config.get("accounts") or {}).get(remote_alias) or {}
        if not account_alias and len(accounts_for_remote) == 1:
            account_alias = next(iter(accounts_for_remote))
        if not account_alias:
            raise ConfigError(f"No active account is selected for remote: {remote_alias}")
        account_alias = _normalize_alias(account_alias, "Account name")

    remote_data = (config.get("remotes") or {}).get(remote_alias)
    account_data = ((config.get("accounts") or {}).get(remote_alias) or {}).get(account_alias)
    if not isinstance(remote_data, dict) or not remote_data.get("url"):
        raise ConfigError(f"Dotori remote does not exist: {remote_alias}")
    if not isinstance(account_data, dict):
        raise ConfigError(f"Dotori account does not exist: {remote_alias}/{account_alias}")
    credential_items = account_data.get("credentials") or {}
    credential_alias = credential
    if not credential_alias:
        if purpose == "sync" and "sync" in credential_items:
            credential_alias = "sync"
        elif DEFAULT_CREDENTIAL in credential_items:
            credential_alias = DEFAULT_CREDENTIAL
        elif len(credential_items) == 1:
            credential_alias = next(iter(credential_items))
    if not credential_alias or credential_alias not in credential_items:
        raise ConfigError(
            f"Credential does not exist for {remote_alias}/{account_alias}: "
            f"{credential_alias or DEFAULT_CREDENTIAL}"
        )
    credential_data = credential_items[credential_alias]
    environment_token = os.environ.get("DOTORI_CLI_TOKEN", "").strip()
    token_key = f"{remote_alias}/{account_alias}/{credential_alias}"
    token = environment_token or (credentials.get("tokens") or {}).get(token_key, "")
    if not token:
        raise ConfigError(f"No token is stored for credential: {token_key}")
    return ServerProfile(
        name=account_alias,
        server=normalize_server_url(str(remote_data["url"])),
        token=str(token),
        token_type=str(credential_data.get("token_type") or infer_token_type(str(token))),
        remote=remote_alias,
        workspace=account_data.get("workspace"),
        credential=credential_alias,
    )


# Compatibility helpers for existing callers and scripts.
def save_profile(
    name: str,
    server: str,
    token: str,
    *,
    token_type: str = "auto",
) -> ServerProfile:
    normalized_server = normalize_server_url(server)
    config, _ = _load_state()
    matching_remote = next(
        (
            remote
            for remote, data in (config.get("remotes") or {}).items()
            if isinstance(data, dict) and data.get("url") == normalized_server
        ),
        None,
    )
    if matching_remote is None:
        preferred = DEFAULT_REMOTE if DEFAULT_REMOTE not in config.get("remotes", {}) else name
        matching_remote = _unique_alias(preferred, set(config.get("remotes", {})))
        add_remote(matching_remote, normalized_server)
    credential = "sync" if _validate_token(token, token_type) == "sync" else DEFAULT_CREDENTIAL
    return save_account(
        matching_remote,
        name,
        token,
        token_type=token_type,
        credential=credential,
    )


def load_profile(name: str | None = None) -> ServerProfile:
    return load_context(legacy_profile=name) if name else load_context()


def list_profiles() -> tuple[str | None, list[dict]]:
    config, _ = _load_state()
    active = config.get("active") or {}
    items = []
    for remote, accounts in sorted((config.get("accounts") or {}).items()):
        for account, data in sorted((accounts or {}).items()):
            credentials = data.get("credentials") or {}
            selected = credentials.get(DEFAULT_CREDENTIAL)
            if selected is None and credentials:
                selected = next(iter(credentials.values()))
            items.append({
                "name": account,
                "remote": remote,
                "server": (config.get("remotes") or {}).get(remote, {}).get("url", ""),
                "token_type": (selected or {}).get("token_type", "cli"),
                "active": remote == active.get("remote") and account == active.get("account"),
            })
    return active.get("account"), items
