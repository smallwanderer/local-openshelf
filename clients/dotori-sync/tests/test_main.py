import httpx

from dotori_cli.config import add_remote, save_account
from dotori_cli.http_client import DotoriClient

from dotori_sync import windows_menu
from dotori_sync.__main__ import EXIT_CONFIG, EXIT_OK, _run_watch, build_parser, main


def test_parser_exposes_watch_command_and_flags():
    parser = build_parser()
    help_text = parser.format_help()
    assert "watch" in help_text
    assert "install-menu" in help_text
    assert "uninstall-menu" in help_text

    watch_help = parser.parse_args(["watch", ".", "--once"])
    assert watch_help.command == "watch"
    assert watch_help.once is True
    assert watch_help.debounce == 5.0
    assert watch_help.interval == 300.0


def test_install_and_uninstall_menu_do_not_require_a_configured_account(fake_winreg, monkeypatch, capsys):
    monkeypatch.setattr(windows_menu.sys, "argv", [r"C:\bin\dotori-sync.exe"])

    assert main(["install-menu"]) == EXIT_OK
    assert windows_menu._BACKGROUND_KEY in fake_winreg.values
    assert "Added" in capsys.readouterr().out

    assert main(["uninstall-menu"]) == EXIT_OK
    assert fake_winreg.existing_keys == set()


def _configure_sync_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path / "config"))
    add_remote("origin", "https://dotori.example.com")
    save_account("origin", "personal", "a" * 64, token_type="sync", credential="sync")


def test_main_reports_config_error_when_nothing_is_registered(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path / "config"))

    exit_code = main(["watch", str(tmp_path), "--once"])

    assert exit_code == EXIT_CONFIG
    assert "Configuration error" in capsys.readouterr().err


def test_main_rejects_a_path_that_is_not_a_directory(tmp_path, monkeypatch, capsys):
    _configure_sync_account(tmp_path, monkeypatch)
    missing = tmp_path / "does-not-exist"

    exit_code = main(["watch", str(missing), "--once"])

    assert exit_code == EXIT_CONFIG
    assert "not a directory" in capsys.readouterr().err


def test_run_watch_once_applies_sync_through_a_real_client(tmp_path):
    (tmp_path / "report.txt").write_text("hello", encoding="utf-8")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/diff/"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "actions": [{"action": "upload", "rel_path": "report.txt"}],
                    "sync_id": "sync-1",
                    "root_name": "docs",
                    "root_uid": "root-1",
                },
            )
        if request.url.path.endswith("/upload/"):
            return httpx.Response(200, json={"ok": True, "node_uid": "file-1", "root_uid": "root-1"})
        if request.url.path.endswith("/confirm/"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False})

    from dotori_cli.config import ServerProfile

    profile = ServerProfile("personal", "https://dotori.example.com", "a" * 64, token_type="sync")
    parser = build_parser()
    args = parser.parse_args(["watch", str(tmp_path), "--root-name", "docs", "--once", "--json"])

    with DotoriClient(profile, transport=httpx.MockTransport(handler)) as client:
        exit_code = _run_watch(args, client)

    assert exit_code == EXIT_OK
    assert [r.url.path for r in requests] == [
        "/api/sync/v1/diff/",
        "/api/sync/v1/upload/",
        "/api/sync/v1/confirm/",
    ]
