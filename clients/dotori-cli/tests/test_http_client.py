import json

import httpx

from dotori_cli.config import ServerProfile
from dotori_cli.http_client import DotoriClient


def test_client_sends_bearer_token_and_uses_cli_routes(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/documents/"):
            return httpx.Response(200, json={"ok": True, "files": [], "total": 0})
        return httpx.Response(200, json={"ok": True, "operation_mode": "search"})

    profile = ServerProfile("home", "https://dotori.example.com", "dtr_cli_secret")
    with DotoriClient(profile, transport=httpx.MockTransport(handler)) as client:
        assert client.status()["operation_mode"] == "search"
        assert client.list_documents()["total"] == 0

    assert [request.url.path for request in requests] == [
        "/api/cli/v1/status/",
        "/api/cli/v1/documents/",
    ]
    assert all(request.headers["authorization"] == "Bearer dtr_cli_secret" for request in requests)


def test_ask_parses_ndjson_events():
    content = "\n".join(
        [
            json.dumps({"type": "started", "job_id": 1}),
            json.dumps({"type": "token", "text": "hello"}),
            json.dumps({"type": "completed", "answer": "hello"}),
        ]
    ) + "\n"

    def handler(request):
        return httpx.Response(
            200,
            content=content.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        )

    profile = ServerProfile("home", "https://dotori.example.com", "dtr_cli_secret")
    with DotoriClient(profile, transport=httpx.MockTransport(handler)) as client:
        events = list(client.ask("question"))

    assert [event["type"] for event in events] == ["started", "token", "completed"]


def test_sync_client_uses_sync_routes_and_root_contract(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/diff/"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "actions": [],
                    "sync_id": "sync-1",
                    "root_name": "docs",
                    "root_uid": "root-1",
                },
            )
        return httpx.Response(200, json={"ok": True, "message": "pong"})

    profile = ServerProfile("folder", "https://dotori.example.com", "sync-secret", "sync")
    with DotoriClient(profile, transport=httpx.MockTransport(handler)) as client:
        assert client.sync_ping()["message"] == "pong"
        assert client.sync_diff(root_name="docs", entries=[])["root_uid"] == "root-1"

    assert [request.url.path for request in requests] == [
        "/api/sync/v1/ping/",
        "/api/sync/v1/diff/",
    ]


def test_identity_uses_the_route_for_the_credential_type():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "account": {"id": "1"}})

    cli = ServerProfile("user", "https://dotori.example.com", "dtr_cli_secret", "cli")
    sync = ServerProfile("user", "https://dotori.example.com", "a" * 64, "sync")
    with DotoriClient(cli, transport=httpx.MockTransport(handler)) as client:
        client.identity()
    with DotoriClient(sync, transport=httpx.MockTransport(handler)) as client:
        client.identity()

    assert paths == ["/api/cli/v1/identity/", "/api/sync/v1/identity/"]
