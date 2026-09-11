from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import httpx

from .config import ServerProfile


class DotoriClientError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class DotoriClient:
    def __init__(
        self,
        profile: ServerProfile,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self._client = httpx.Client(
            base_url=f"{profile.server}/",
            headers={
                "Authorization": f"Bearer {profile.token}",
                "Accept": "application/json",
                "User-Agent": "dotori-cli/0.3.0",
            },
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, read=360.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DotoriClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @staticmethod
    def _error_from_response(response: httpx.Response) -> DotoriClientError:
        code = ""
        message = f"Dotori returned HTTP {response.status_code}."
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "")
                message = str(error.get("message") or message)
            elif isinstance(payload.get("errors"), list) and payload["errors"]:
                message = str(payload["errors"][0])
                code = str(payload.get("code") or "")
        return DotoriClientError(
            message,
            status_code=response.status_code,
            code=code,
        )

    def _request_json(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DotoriClientError(f"Cannot connect to {self.profile.server}: {exc}") from exc
        if response.is_redirect:
            raise DotoriClientError(
                "Dotori redirected the CLI API request; check the configured server URL.",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise self._error_from_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DotoriClientError("Dotori returned an invalid JSON response.") from exc
        if not isinstance(payload, dict):
            raise DotoriClientError("Dotori returned an unexpected JSON response.")
        return payload

    def status(self) -> dict:
        return self._request_json("GET", "api/cli/v1/status/")

    def identity(self) -> dict:
        path = (
            "api/sync/v1/identity/"
            if self.profile.token_type == "sync"
            else "api/cli/v1/identity/"
        )
        return self._request_json("GET", path)

    def list_documents(
        self,
        *,
        parent_id: str = "",
        query: str = "",
        page: int = 1,
        limit: int = 50,
    ) -> dict:
        params = {"page": page, "limit": limit}
        if parent_id:
            params["parent_id"] = parent_id
        if query:
            params["q"] = query
        return self._request_json("GET", "api/cli/v1/documents/", params=params)

    def upload(self, file_path: Path, *, parent_id: str = "") -> dict:
        if not file_path.is_file():
            raise DotoriClientError(f"File does not exist: {file_path}")
        data = {"parent_id": parent_id} if parent_id else {}
        with file_path.open("rb") as handle:
            return self._request_json(
                "POST",
                "api/cli/v1/upload/",
                data=data,
                files={"file": (file_path.name, handle)},
            )

    def sync_ping(self) -> dict:
        return self._request_json("GET", "api/sync/v1/ping/")

    def sync_diff(self, *, root_name: str, entries: list[dict]) -> dict:
        return self._request_json(
            "POST",
            "api/sync/v1/diff/",
            json={"root_name": root_name, "entries": entries},
        )

    def sync_mkdir(
        self,
        *,
        root_name: str,
        root_uid: str,
        sync_id: str,
        rel_path: str,
    ) -> dict:
        return self._request_json(
            "POST",
            "api/sync/v1/mkdir/",
            json={
                "root_name": root_name,
                "root_uid": root_uid,
                "sync_id": sync_id,
                "rel_path": rel_path,
            },
        )

    def sync_upload(
        self,
        file_path: Path,
        *,
        root_name: str,
        root_uid: str,
        sync_id: str,
        rel_path: str,
        content_hash: str,
        ai_processing_enabled: bool,
    ) -> dict:
        with file_path.open("rb") as handle:
            return self._request_json(
                "POST",
                "api/sync/v1/upload/",
                data={
                    "root_name": root_name,
                    "root_uid": root_uid,
                    "sync_id": sync_id,
                    "rel_path": rel_path,
                    "content_hash": content_hash,
                    "ai_processing_enabled": "1" if ai_processing_enabled else "0",
                },
                files={"file": (file_path.name, handle)},
            )

    def sync_delete(self, *, root_uid: str, sync_id: str, node_uids: list[str]) -> dict:
        return self._request_json(
            "POST",
            "api/sync/v1/delete/",
            json={"root_uid": root_uid, "sync_id": sync_id, "node_uids": node_uids},
        )

    def sync_confirm(self, *, sync_id: str, results: list[dict]) -> dict:
        return self._request_json(
            "POST",
            "api/sync/v1/confirm/",
            json={"sync_id": sync_id, "results": results},
        )

    def search(
        self,
        query: str,
        *,
        mode: str = "basic",
        node_ids: list[str] | None = None,
        top_k: int = 5,
    ) -> dict:
        return self._request_json(
            "POST",
            "api/cli/v1/search/",
            json={
                "query": query,
                "mode": mode,
                "node_ids": node_ids or [],
                "top_k": top_k,
            },
        )

    def ask(
        self,
        question: str,
        *,
        node_ids: list[str] | None = None,
        language: str = "ko",
        top_k: int = 3,
    ) -> Iterator[dict]:
        try:
            with self._client.stream(
                "POST",
                "api/cli/v1/ask/stream/",
                json={
                    "question": question,
                    "node_ids": node_ids or [],
                    "language": language,
                    "top_k": top_k,
                },
                headers={"Accept": "application/x-ndjson"},
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._error_from_response(response)
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as exc:
                        raise DotoriClientError("Dotori returned an invalid NDJSON event.") from exc
                    if isinstance(event, dict):
                        yield event
        except DotoriClientError:
            raise
        except httpx.HTTPError as exc:
            raise DotoriClientError(f"RAG stream failed: {exc}") from exc
