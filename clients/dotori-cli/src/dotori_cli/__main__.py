from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .config import (
    ConfigError,
    DEFAULT_CREDENTIAL,
    DEFAULT_REMOTE,
    ServerProfile,
    add_remote,
    get_remote,
    infer_token_type,
    list_accounts,
    list_profiles,
    list_remotes,
    load_context,
    normalize_server_url,
    save_account,
    set_remote_url,
    use_account,
)
from .http_client import DotoriClient, DotoriClientError
from .sync import SyncPlanError, run_sync


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_NETWORK = 5
EXIT_SERVER = 6


def _add_output_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dotori", description="Dotori HTTP client")
    parser.add_argument("--remote", help="Named Dotori server remote.")
    parser.add_argument("--account", help="Account alias under the selected remote.")
    parser.add_argument("--credential", help="Credential alias under the selected account.")
    parser.add_argument("--profile", help="Deprecated account-profile compatibility alias.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="Register a self-hosted Dotori server.")
    connect.add_argument("server")
    connect.add_argument("--name", help="Deprecated alias for --account.")
    connect.add_argument("--remote", default=DEFAULT_REMOTE)
    connect.add_argument("--account")
    connect.add_argument("--credential")
    connect.add_argument("--token", help="Access token; omit to enter it securely.")
    connect.add_argument(
        "--token-type",
        choices=("auto", "cli", "sync"),
        default="auto",
        help="Token capability to verify; auto detects the token format.",
    )
    connect.add_argument("--skip-verify", action="store_true", help="Save without probing status.")
    _add_output_option(connect)

    profiles = subparsers.add_parser("profiles", help="List configured server profiles.")
    _add_output_option(profiles)

    remote = subparsers.add_parser("remote", help="Manage reusable Dotori server addresses.")
    remote_commands = remote.add_subparsers(dest="remote_command", required=True)
    remote_add = remote_commands.add_parser("add", help="Add a named server address.")
    remote_add.add_argument("name")
    remote_add.add_argument("url")
    _add_output_option(remote_add)
    remote_list = remote_commands.add_parser("list", help="List server addresses.")
    _add_output_option(remote_list)
    remote_get = remote_commands.add_parser("get-url", help="Print one server address.")
    remote_get.add_argument("name")
    _add_output_option(remote_get)
    remote_set = remote_commands.add_parser("set-url", help="Change one server address.")
    remote_set.add_argument("name")
    remote_set.add_argument("url")
    _add_output_option(remote_set)

    account = subparsers.add_parser("account", help="Manage accounts under a remote.")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    account_add = account_commands.add_parser("add", help="Add or update an account credential.")
    account_add.add_argument("name")
    account_add.add_argument("--remote")
    account_add.add_argument("--credential")
    account_add.add_argument("--token", help="Access token; omit to enter it securely.")
    account_add.add_argument(
        "--token-type",
        choices=("auto", "cli", "sync"),
        default="auto",
    )
    account_add.add_argument("--skip-verify", action="store_true")
    _add_output_option(account_add)
    account_list = account_commands.add_parser("list", help="List accounts for a remote.")
    account_list.add_argument("--remote")
    _add_output_option(account_list)
    account_use = account_commands.add_parser("use", help="Select the active account.")
    account_use.add_argument("name")
    account_use.add_argument("--remote")
    _add_output_option(account_use)

    status = subparsers.add_parser("status", help="Show server AI and runtime status.")
    _add_output_option(status)

    documents = subparsers.add_parser("list", help="List accessible documents and folders.")
    documents.add_argument("--parent", default="")
    documents.add_argument("--query", default="")
    documents.add_argument("--page", type=int, default=1)
    documents.add_argument("--limit", type=int, default=50)
    _add_output_option(documents)

    upload = subparsers.add_parser("upload", help="Upload one document.")
    upload.add_argument("file", type=Path)
    upload.add_argument("--parent", default="")
    _add_output_option(upload)

    search = subparsers.add_parser("search", help="Search documents.")
    search.add_argument("query")
    search.add_argument("--advanced", action="store_true")
    search.add_argument("--node", action="append", default=[])
    search.add_argument("--top-k", type=int, default=5)
    _add_output_option(search)

    ask = subparsers.add_parser("ask", help="Stream a RAG answer.")
    ask.add_argument("question")
    ask.add_argument("--node", action="append", default=[])
    ask.add_argument("--language", default="ko")
    ask.add_argument("--top-k", type=int, default=3)
    _add_output_option(ask)

    sync = subparsers.add_parser("sync", help="Plan or apply one-way local folder sync.")
    sync.add_argument("path", type=Path)
    sync.add_argument("--root-name", default="", help="Unique server folder name under /sync.")
    sync.add_argument("--apply", action="store_true", help="Apply the planned changes.")
    sync.add_argument(
        "--delete",
        action="store_true",
        help="Allow --apply to move server-only entries to trash.",
    )
    sync.add_argument("--no-ai", action="store_true", help="Disable AI processing for new uploads.")
    _add_output_option(sync)
    return parser


def _json_output(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _read_token(value: str | None) -> str:
    return (
        value
        or os.environ.get("DOTORI_CLI_TOKEN", "").strip()
        or getpass.getpass("Dotori access token: ").strip()
    )


def _identity_for_token(
    *,
    remote: str,
    account: str,
    server: str,
    token: str,
    token_type: str,
    skip_verify: bool,
) -> dict:
    if skip_verify:
        return {}
    candidate = ServerProfile(
        name=account,
        server=server,
        token=token,
        token_type=token_type,
        remote=remote,
    )
    with DotoriClient(candidate) as client:
        return client.identity()


def _run_connect(args) -> int:
    token = _read_token(args.token)
    token_type = infer_token_type(token) if args.token_type == "auto" else args.token_type
    if token_type == "cli" and not token.startswith("dtr_cli_"):
        raise ConfigError("Expected a Dotori CLI token beginning with 'dtr_cli_'.")
    server = normalize_server_url(args.server)
    remote = args.remote or DEFAULT_REMOTE
    account = args.account or args.name or "default"
    credential = args.credential or ("sync" if token_type == "sync" else DEFAULT_CREDENTIAL)
    add_remote(remote, server, allow_existing=True)
    identity = _identity_for_token(
        remote=remote,
        account=account,
        server=server,
        token=token,
        token_type=token_type,
        skip_verify=args.skip_verify,
    )
    profile = save_account(
        remote,
        account,
        token,
        token_type=token_type,
        credential=credential,
        identity=identity,
    )
    payload = {
        "ok": True,
        "remote": profile.remote,
        "account": profile.name,
        "credential": profile.credential,
        "server": profile.server,
        "token_type": profile.token_type,
        "workspace": profile.workspace,
    }
    if args.json:
        _json_output(payload)
    else:
        print(
            f"Connected {profile.remote}/{profile.name} "
            f"with credential '{profile.credential}' to {profile.server}"
        )
    return EXIT_OK


def _run_remote_config(args) -> int:
    if args.remote_command == "add":
        payload = add_remote(args.name, args.url)
        if args.json:
            _json_output({"ok": True, "remote": payload})
        else:
            print(f"Added remote '{payload['name']}' -> {payload['url']}")
    elif args.remote_command == "list":
        active, items = list_remotes()
        if args.json:
            _json_output({"active_remote": active, "remotes": items})
        elif not items:
            print("No Dotori remotes configured.")
        else:
            for item in items:
                marker = "*" if item["active"] else " "
                print(f"{marker} {item['name']:<16} {item['url']}")
    elif args.remote_command == "get-url":
        payload = get_remote(args.name)
        _json_output({"ok": True, "remote": payload}) if args.json else print(payload["url"])
    elif args.remote_command == "set-url":
        payload = set_remote_url(args.name, args.url)
        if args.json:
            _json_output({"ok": True, "remote": payload})
        else:
            print(f"Updated remote '{payload['name']}' -> {payload['url']}")
    return EXIT_OK


def _selected_remote(value: str | None) -> str:
    if value:
        return value
    active, _ = list_remotes()
    if not active:
        raise ConfigError("No active remote is selected. Run 'dotori remote add' first.")
    return active


def _run_account_config(args) -> int:
    remote = _selected_remote(args.remote)
    if args.account_command == "add":
        token = _read_token(args.token)
        token_type = infer_token_type(token) if args.token_type == "auto" else args.token_type
        if token_type == "cli" and not token.startswith("dtr_cli_"):
            raise ConfigError("Expected a Dotori CLI token beginning with 'dtr_cli_'.")
        server = get_remote(remote)["url"]
        credential = args.credential or (
            "sync" if token_type == "sync" else DEFAULT_CREDENTIAL
        )
        identity = _identity_for_token(
            remote=remote,
            account=args.name,
            server=server,
            token=token,
            token_type=token_type,
            skip_verify=args.skip_verify,
        )
        profile = save_account(
            remote,
            args.name,
            token,
            token_type=token_type,
            credential=credential,
            identity=identity,
        )
        payload = {
            "ok": True,
            "remote": profile.remote,
            "account": profile.name,
            "credential": profile.credential,
            "server": profile.server,
            "token_type": profile.token_type,
            "workspace": profile.workspace,
        }
        if args.json:
            _json_output(payload)
        else:
            print(
                f"Saved credential '{profile.credential}' for "
                f"{profile.remote}/{profile.name}"
            )
    elif args.account_command == "list":
        active, items = list_accounts(remote)
        if args.json:
            _json_output({"active": active, "accounts": items})
        elif not items:
            print(f"No Dotori accounts configured for remote '{remote}'.")
        else:
            for item in items:
                marker = "*" if item["active"] else " "
                label = item.get("display_name") or item.get("email") or "-"
                credentials = ",".join(sorted(item.get("credentials") or {}))
                print(f"{marker} {item['name']:<16} {label:<24} [{credentials}]")
    elif args.account_command == "use":
        payload = use_account(remote, args.name)
        if args.json:
            _json_output({"ok": True, "active": payload})
        else:
            print(f"Using {payload['remote']}/{payload['account']}")
    return EXIT_OK


def _run_profiles(args) -> int:
    active, profiles = list_profiles()
    if args.json:
        _json_output({"active_profile": active, "profiles": profiles})
    elif not profiles:
        print("No Dotori profiles configured.")
    else:
        for profile in profiles:
            marker = "*" if profile["active"] else " "
            print(
                f"{marker} {profile.get('remote', DEFAULT_REMOTE)}/{profile['name']:<16} "
                f"{profile.get('token_type', 'cli'):<4} {profile['server']}"
            )
    return EXIT_OK


def _print_status(payload: dict) -> None:
    print(f"Operation mode: {payload.get('operation_mode', '-')}")
    for name in ("embedding", "rag"):
        item = payload.get(name) or {}
        print(f"{name.capitalize():<10}: {item.get('status', 'unknown')} ({item.get('model', '-')})")


def _print_sync(payload: dict) -> None:
    summary = payload.get("summary") or {}
    print(
        f"Sync {payload.get('mode', 'dry-run')}: {payload.get('local_root', '-')} "
        f"-> /sync/{payload.get('root_name', '-')}"
    )
    print(
        "Plan: "
        f"mkdir={summary.get('mkdir', 0)} "
        f"upload={summary.get('upload', 0)} "
        f"update={summary.get('update', 0)} "
        f"delete={summary.get('delete', 0)} "
        f"conflict={summary.get('conflict', 0)}"
    )
    for action in payload.get("actions", []):
        print(f"{str(action.get('action', '')).upper():<8} {action.get('rel_path', '')}")
    if payload.get("mode") == "dry-run":
        print("No files were changed. Re-run with --apply; add --delete to allow deletions.")
    else:
        print(
            f"Applied={payload.get('applied', 0)} "
            f"failed={payload.get('failed', 0)} skipped={payload.get('skipped', 0)}"
        )


def _run_remote(args, client: DotoriClient) -> int:
    if args.command == "status":
        payload = client.status()
        _json_output(payload) if args.json else _print_status(payload)
    elif args.command == "list":
        payload = client.list_documents(
            parent_id=args.parent,
            query=args.query,
            page=args.page,
            limit=args.limit,
        )
        if args.json:
            _json_output(payload)
        else:
            for item in payload.get("files", []):
                kind = "DIR " if item.get("node_type") == "folder" else "FILE"
                print(f"{kind}  {item.get('uid', '')}  {item.get('name', '')}")
            print(f"{payload.get('total', 0)} item(s)")
    elif args.command == "upload":
        payload = client.upload(args.file, parent_id=args.parent)
        if args.json:
            _json_output(payload)
        else:
            item = payload.get("file") or {}
            print(f"Uploaded {item.get('name', args.file.name)} ({item.get('uid', '-')})")
    elif args.command == "search":
        payload = client.search(
            args.query,
            mode="advanced" if args.advanced else "basic",
            node_ids=args.node,
            top_k=args.top_k,
        )
        if args.json:
            _json_output(payload)
        else:
            for index, result in enumerate(payload.get("results", []), start=1):
                score = result.get("doc_score")
                score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "-"
                print(f"{index}. {result.get('node_name', '-')}  score={score_text}")
                evidences = result.get("evidences") or []
                if evidences:
                    text = str(evidences[0].get("text") or "").replace("\n", " ")
                    print(f"   {text[:180]}")
    elif args.command == "ask":
        events = []
        terminal_error = None
        for event in client.ask(
            args.question,
            node_ids=args.node,
            language=args.language,
            top_k=args.top_k,
        ):
            events.append(event)
            if not args.json and event.get("type") == "token":
                print(event.get("text", ""), end="", flush=True)
            if event.get("type") == "error":
                terminal_error = event
        if args.json:
            _json_output({"events": events})
        else:
            print()
        if terminal_error:
            raise DotoriClientError(str(terminal_error.get("message") or "RAG failed."))
    elif args.command == "sync":
        payload = run_sync(
            client,
            args.path,
            root_name=args.root_name,
            apply=args.apply,
            allow_delete=args.delete,
            ai_processing_enabled=not args.no_ai,
        )
        _json_output(payload) if args.json else _print_sync(payload)
        if not payload.get("ok"):
            return EXIT_SERVER
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "connect":
            return _run_connect(args)
        if args.command == "remote":
            return _run_remote_config(args)
        if args.command == "account":
            return _run_account_config(args)
        if args.command == "profiles":
            return _run_profiles(args)
        if args.profile and (args.remote or args.account):
            raise ConfigError("Use either --profile or --remote/--account, not both.")
        profile = load_context(
            remote=args.remote,
            account=args.account,
            credential=args.credential,
            purpose="sync" if args.command == "sync" else "default",
            legacy_profile=args.profile,
        )
        with DotoriClient(profile) as client:
            return _run_remote(args, client)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DotoriClientError as exc:
        print(f"Dotori error: {exc.message}", file=sys.stderr)
        if exc.status_code in {401, 403}:
            return EXIT_AUTH
        if exc.status_code is None:
            return EXIT_NETWORK
        return EXIT_SERVER
    except SyncPlanError as exc:
        print(f"Sync error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
