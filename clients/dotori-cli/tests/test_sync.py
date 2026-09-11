from pathlib import Path

from dotori_cli.sync import build_manifest, run_sync


class FakeSyncClient:
    def __init__(self, actions):
        self.actions = actions
        self.calls = []

    def sync_diff(self, *, root_name, entries):
        self.calls.append(("diff", root_name, entries))
        return {
            "ok": True,
            "actions": self.actions,
            "sync_id": "sync-1",
            "root_name": root_name,
            "root_uid": "",
        }

    def sync_mkdir(self, **kwargs):
        self.calls.append(("mkdir", kwargs))
        return {"ok": True, "root_uid": "root-1", "node_uid": "folder-1"}

    def sync_upload(self, file_path: Path, **kwargs):
        self.calls.append(("upload", file_path, kwargs))
        return {"ok": True, "root_uid": "root-1", "node_uid": "file-1"}

    def sync_delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {"ok": True, "root_uid": "root-1", "deleted": 1}

    def sync_confirm(self, **kwargs):
        self.calls.append(("confirm", kwargs))
        return {"ok": True}


def test_manifest_contains_relative_directories_and_file_hash(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    document = folder / "report.txt"
    document.write_text("hello", encoding="utf-8")

    root, entries = build_manifest(tmp_path)

    assert root == tmp_path.resolve()
    assert [entry["rel_path"] for entry in entries] == ["docs", "docs/report.txt"]
    assert entries[1]["content_hash"] == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_sync_is_dry_run_by_default(tmp_path):
    (tmp_path / "report.txt").write_text("hello", encoding="utf-8")
    client = FakeSyncClient([{"action": "upload", "rel_path": "report.txt"}])

    payload = run_sync(client, tmp_path, root_name="docs")

    assert payload["mode"] == "dry-run"
    assert [call[0] for call in client.calls] == ["diff"]


def test_apply_propagates_created_root_and_skips_delete_without_flag(tmp_path):
    (tmp_path / "report.txt").write_text("hello", encoding="utf-8")
    client = FakeSyncClient(
        [
            {"action": "mkdir", "rel_path": "nested"},
            {"action": "upload", "rel_path": "report.txt"},
            {"action": "delete", "rel_path": "old.txt", "server_node_uid": "old-1"},
        ]
    )

    payload = run_sync(client, tmp_path, root_name="docs", apply=True)

    assert payload["ok"] is True
    assert payload["applied"] == 2
    assert payload["skipped"] == 1
    assert [call[0] for call in client.calls] == ["diff", "mkdir", "upload", "confirm"]
    upload_call = client.calls[2]
    assert upload_call[2]["root_uid"] == "root-1"


def test_apply_delete_requires_explicit_flag(tmp_path):
    client = FakeSyncClient(
        [{"action": "delete", "rel_path": "old.txt", "server_node_uid": "old-1"}]
    )
    client.sync_diff = lambda **kwargs: {
        "ok": True,
        "actions": client.actions,
        "sync_id": "sync-1",
        "root_name": "docs",
        "root_uid": "root-1",
    }

    payload = run_sync(client, tmp_path, root_name="docs", apply=True, allow_delete=True)

    assert payload["applied"] == 1
    assert [call[0] for call in client.calls] == ["delete", "confirm"]
