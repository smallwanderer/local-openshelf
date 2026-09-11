from dotori_cli.__main__ import EXIT_CONFIG, build_parser, main


def test_parser_exposes_expected_user_commands():
    parser = build_parser()
    help_text = parser.format_help()
    for command in (
        "connect", "remote", "account", "profiles", "status", "list", "upload", "search", "ask", "sync"
    ):
        assert command in help_text


def test_connect_rejects_non_cli_token_before_saving(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))

    exit_code = main(
        [
            "connect",
            "https://dotori.example.com",
            "--token",
            "legacy-token",
            "--token-type",
            "cli",
            "--skip-verify",
        ]
    )

    assert exit_code == EXIT_CONFIG
    assert "dtr_cli_" in capsys.readouterr().err
    assert not (tmp_path / "config.json").exists()


def test_connect_accepts_sync_token_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))

    exit_code = main(
        [
            "connect",
            "https://dotori.example.com",
            "--token",
            "a" * 64,
            "--token-type",
            "sync",
            "--skip-verify",
        ]
    )

    assert exit_code == 0
    assert "credential 'sync'" in capsys.readouterr().out


def test_remote_and_account_commands_build_active_context(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOTORI_CLI_CONFIG_DIR", str(tmp_path))

    assert main(["remote", "add", "origin", "https://dotori.example.com"]) == 0
    assert main([
        "account", "add", "personal", "--remote", "origin",
        "--token", "dtr_cli_secret", "--skip-verify",
    ]) == 0
    assert main(["account", "use", "personal", "--remote", "origin"]) == 0
    assert main(["remote", "get-url", "origin"]) == 0

    output = capsys.readouterr().out
    assert "https://dotori.example.com" in output
    assert "origin/personal" in output
