"""HTTP transport used by the queue-only orchestrator.

The module contains no parser/model imports so it remains safe in the
lightweight orchestrator image.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

from document_ai.orchestration import WorkKind


@dataclass(frozen=True)
class ExecutorDispatchError(RuntimeError):
    message: str
    retryable: bool = True
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        return self.message


def _base_url(kind: WorkKind) -> str:
    if kind is WorkKind.PARSE_DOCUMENT:
        return os.getenv("PARSER_EXECUTOR_URL", "http://parser-executor:8002").rstrip("/")
    return os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-executor:8001").rstrip("/")


def _timeout_seconds() -> float:
    try:
        return float(os.getenv("EXECUTOR_DISPATCH_TIMEOUT_SECONDS", "1830"))
    except ValueError:
        return 1830.0


def _headers() -> dict[str, str]:
    token = settings.EXECUTOR_INTERNAL_TOKEN
    if not token:
        raise ExecutorDispatchError("EXECUTOR_INTERNAL_TOKEN is not configured", retryable=False)
    return {"Authorization": f"Bearer {token}"}


def _retry_after(response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def dispatch_executor_job(kind: WorkKind, *, envelope: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    body = {**envelope, "work_kind": kind.value, "payload": payload}
    if envelope.get("attempt", 1) > 1:
        existing = read_executor_job(kind, envelope["job_id"])
        if existing.get("status") == "running":
            raise ExecutorDispatchError("Executor job is still running", retry_after_seconds=5)
        if existing.get("status") == "succeeded":
            return existing
        if existing.get("status") == "failed" and not existing.get("retryable"):
            raise ExecutorDispatchError(str(existing.get("error")), retryable=False)
    try:
        response = requests.post(
            f"{_base_url(kind)}/internal/v1/jobs/execute/",
            json=body,
            headers=_headers(),
            timeout=(5, min(_timeout_seconds(), max(1, envelope.get("deadline_at", time.time() + 1800) - time.time()) + 30)),
        )
    except requests.RequestException as exc:
        raise ExecutorDispatchError(f"{kind.value} executor dispatch failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise ExecutorDispatchError(
            f"{kind.value} executor returned malformed response ({response.status_code})",
            retryable=response.status_code >= 500,
        ) from exc

    if response.status_code == 202:
        raise ExecutorDispatchError(
            f"{kind.value} executor job is still running",
            retryable=True,
            retry_after_seconds=_retry_after(response) or 5.0,
        )
    if response.status_code >= 400 or not body.get("ok", False):
        error = body.get("error") or {}
        code = error.get("code", f"HTTP_{response.status_code}")
        message = error.get("message", code)
        raise ExecutorDispatchError(
            f"{kind.value} executor error {code}: {message}",
            retryable=bool(body.get("retryable", response.status_code >= 500)),
            retry_after_seconds=_retry_after(response),
        )
    return body


def read_executor_job(kind: WorkKind, job_id: str) -> dict[str, Any]:
    try:
        response = requests.get(
            f"{_base_url(kind)}/internal/v1/jobs/{job_id}/",
            headers=_headers(),
            timeout=10,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise ExecutorDispatchError(f"{kind.value} executor receipt lookup failed: {exc}") from exc
