from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotori_cli.config import ConfigError, load_context
from dotori_cli.http_client import DotoriClient, DotoriClientError

from .daemon import SyncRunner, WatchLoop
from .watcher import start_observer

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_NETWORK = 5
EXIT_SERVER = 6

logger = logging.getLogger("dotori_sync")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dotori-sync",
        description=(
            "Background folder watcher for one-way Dotori sync. Reuses the "
            "same remote/account/credential configuration as dotori-cli; "
            "register a 'sync' credential with 'dotori account add "
            "--credential sync --token-type sync' first."
        ),
    )
    parser.add_argument("--remote", help="Named Dotori server remote.")
    parser.add_argument("--account", help="Account alias under the selected remote.")
    parser.add_argument(
        "--credential",
        help="Credential alias; defaults to the 'sync' credential if one is registered.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    watch = subparsers.add_parser(
        "watch",
        help="Watch a local folder and continuously apply one-way sync to the server.",
    )
    watch.add_argument("path", type=Path)
    watch.add_argument("--root-name", default="", help="Unique server folder name under /sync.")
    watch.add_argument(
        "--debounce",
        type=float,
        default=5.0,
        help="Seconds of filesystem quiet before a change triggers a sync pass (default: 5).",
    )
    watch.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help=(
            "Fallback sync interval in seconds, run even without observed "
            "filesystem events (default: 300)."
        ),
    )
    watch.add_argument(
        "--delete",
        action="store_true",
        help="Allow sync passes to move server-only entries to trash.",
    )
    watch.add_argument("--no-ai", action="store_true", help="Disable AI processing for new uploads.")
    watch.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync pass and exit, without starting the watcher.",
    )
    watch.add_argument("--json", action="store_true", help="Print machine-readable JSON for --once.")
    watch.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")

    subparsers.add_parser(
        "install-menu",
        help="Add 'Sync with Dotori' to the Windows Explorer right-click menu.",
    )
    subparsers.add_parser(
        "uninstall-menu",
        help="Remove the Windows Explorer right-click menu entry.",
    )

    return parser


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _run_watch(args: argparse.Namespace, client: DotoriClient) -> int:
    root = args.path.expanduser().resolve()
    if not root.is_dir():
        print(f"Sync error: not a directory: {root}", file=sys.stderr)
        return EXIT_CONFIG

    root_name = (args.root_name or root.name).strip()
    runner = SyncRunner(
        client,
        root,
        root_name=root_name,
        allow_delete=args.delete,
        ai_processing_enabled=not args.no_ai,
    )

    if args.once:
        payload = runner.run_once()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK if payload.get("ok") else EXIT_SERVER

    loop = WatchLoop(
        runner,
        debounce_seconds=args.debounce,
        fallback_interval_seconds=args.interval,
    )
    observer = start_observer(root, loop.notify)
    logger.info(
        "Watching %s -> /sync/%s (debounce=%ss, fallback=%ss, delete=%s)",
        root,
        root_name,
        args.debounce,
        args.interval,
        args.delete,
    )
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Stopping.")
    finally:
        observer.stop()
        observer.join()
    return EXIT_OK


def _run_install_menu() -> int:
    from .windows_menu import install_context_menu

    install_context_menu()
    print("Added 'Sync with Dotori' to the Explorer right-click menu.")
    print("Right-click a folder (or inside one) and choose it to start watching that folder.")
    return EXIT_OK


def _run_uninstall_menu() -> int:
    from .windows_menu import uninstall_context_menu

    uninstall_context_menu()
    print("Removed the Explorer right-click menu entry.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    try:
        if args.command == "install-menu":
            return _run_install_menu()
        if args.command == "uninstall-menu":
            return _run_uninstall_menu()
        profile = load_context(
            remote=args.remote,
            account=args.account,
            credential=args.credential,
            purpose="sync",
        )
        with DotoriClient(profile) as client:
            if args.command == "watch":
                return _run_watch(args, client)
            return EXIT_USAGE
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
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
