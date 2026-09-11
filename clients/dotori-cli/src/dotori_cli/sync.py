from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .http_client import DotoriClient, DotoriClientError


class SyncPlanError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path) -> tuple[Path, list[dict]]:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise SyncPlanError(f"Sync root is not a directory: {root}")

    entries = []
    for current, directory_names, file_names in os.walk(resolved_root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name for name in directory_names if not (current_path / name).is_symlink()
        )
        for name in directory_names:
            path = current_path / name
            entries.append({
                "rel_path": path.relative_to(resolved_root).as_posix(),
                "is_dir": True,
                "content_hash": "",
            })
        for name in sorted(file_names):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            before = path.stat()
            content_hash = _sha256(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SyncPlanError(f"File changed while the manifest was being built: {path}")
            entries.append({
                "rel_path": path.relative_to(resolved_root).as_posix(),
                "is_dir": False,
                "content_hash": content_hash,
                "size": after.st_size,
                "mtime_ns": after.st_mtime_ns,
            })
    entries.sort(key=lambda entry: (entry["rel_path"].count("/"), entry["rel_path"]))
    return resolved_root, entries


def _action_summary(actions: list[dict]) -> dict:
    summary = {"mkdir": 0, "upload": 0, "update": 0, "delete": 0, "conflict": 0}
    for action in actions:
        action_name = str(action.get("action") or "")
        summary[action_name] = summary.get(action_name, 0) + 1
    return summary


def _verify_file_unchanged(root: Path, entry: dict) -> Path:
    path = root / entry["rel_path"]
    try:
        stat = path.stat()
    except OSError as exc:
        raise SyncPlanError(f"Cannot read planned upload: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise SyncPlanError(f"Planned upload is no longer a regular file: {path}")
    if (stat.st_size, stat.st_mtime_ns) != (entry.get("size"), entry.get("mtime_ns")):
        raise SyncPlanError(f"File changed after the sync plan was created: {path}")
    return path


def run_sync(
    client: DotoriClient,
    root: Path,
    *,
    root_name: str = "",
    apply: bool = False,
    allow_delete: bool = False,
    ai_processing_enabled: bool = True,
) -> dict:
    resolved_root, entries = build_manifest(root)
    selected_root_name = (root_name or resolved_root.name).strip()
    if not selected_root_name:
        raise SyncPlanError("A non-empty sync root name is required.")

    plan = client.sync_diff(root_name=selected_root_name, entries=entries)
    actions = list(plan.get("actions") or [])
    payload = {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "local_root": str(resolved_root),
        "root_name": str(plan.get("root_name") or selected_root_name),
        "root_uid": str(plan.get("root_uid") or ""),
        "sync_id": str(plan.get("sync_id") or ""),
        "summary": _action_summary(actions),
        "actions": actions,
        "results": [],
    }
    if not apply:
        return payload

    entries_by_path = {entry["rel_path"]: entry for entry in entries}
    root_uid = payload["root_uid"]
    sync_id = payload["sync_id"]
    results = []

    ordered_actions = sorted(
        actions,
        key=lambda action: (
            {"mkdir": 0, "upload": 1, "update": 1, "conflict": 2, "delete": 3}.get(
                str(action.get("action") or ""), 2
            ),
            str(action.get("rel_path") or "").count("/"),
            str(action.get("rel_path") or ""),
        ),
    )
    for action in ordered_actions:
        action_name = str(action.get("action") or "")
        rel_path = str(action.get("rel_path") or "")
        result = {"action": action_name, "rel_path": rel_path, "success": False}
        if action_name == "delete" and not allow_delete:
            result.update({"skipped": True, "error": "Deletion requires --delete."})
            results.append(result)
            continue
        if action_name == "conflict":
            result["error"] = "Local and server entries have different types."
            results.append(result)
            continue
        try:
            if action_name == "mkdir":
                response = client.sync_mkdir(
                    root_name=selected_root_name,
                    root_uid=root_uid,
                    sync_id=sync_id,
                    rel_path=rel_path,
                )
            elif action_name in {"upload", "update"}:
                entry = entries_by_path[rel_path]
                file_path = _verify_file_unchanged(resolved_root, entry)
                response = client.sync_upload(
                    file_path,
                    root_name=selected_root_name,
                    root_uid=root_uid,
                    sync_id=sync_id,
                    rel_path=rel_path,
                    content_hash=str(entry["content_hash"]),
                    ai_processing_enabled=ai_processing_enabled,
                )
            elif action_name == "delete":
                response = client.sync_delete(
                    root_uid=root_uid,
                    sync_id=sync_id,
                    node_uids=[str(action.get("server_node_uid") or "")],
                )
            else:
                result["error"] = f"Unsupported sync action: {action_name}"
                results.append(result)
                continue
            root_uid = str(response.get("root_uid") or root_uid)
            result.update({"success": True, "response": response})
        except (DotoriClientError, KeyError) as exc:
            result["error"] = str(getattr(exc, "message", exc))
        results.append(result)

    client.sync_confirm(sync_id=sync_id, results=results)
    payload["root_uid"] = root_uid
    payload["results"] = results
    payload["ok"] = all(result.get("success") or result.get("skipped") for result in results)
    payload["applied"] = sum(1 for result in results if result.get("success"))
    payload["failed"] = sum(
        1 for result in results if not result.get("success") and not result.get("skipped")
    )
    payload["skipped"] = sum(1 for result in results if result.get("skipped"))
    return payload
