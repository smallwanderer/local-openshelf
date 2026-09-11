from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.decorators import email_verification_required


logger = logging.getLogger(__name__)


class SPABuildUnavailable(RuntimeError):
    pass


def _safe_asset_path(value: object) -> str:
    path = str(value or "").strip().lstrip("/")
    if not path or path.startswith("../") or "/../" in path:
        raise SPABuildUnavailable("The SPA manifest contains an invalid asset path.")
    return f"spa/{path}"


@lru_cache(maxsize=4)
def _load_spa_entry(manifest_path: str) -> tuple[str, tuple[str, ...]]:
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        entry = manifest["index.html"]
        javascript = _safe_asset_path(entry["file"])
        css_entries = entry.get("css", [])
        if not isinstance(css_entries, list):
            raise TypeError("The SPA manifest css entry must be a list.")
        stylesheets = tuple(_safe_asset_path(item) for item in css_entries)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SPABuildUnavailable("The Dotori SPA build manifest is unavailable.") from exc
    return javascript, stylesheets


@never_cache
@require_GET
@login_required
@email_verification_required
def spa_shell(request: HttpRequest, client_path: str = "") -> HttpResponse:
    if not settings.SPA_ENABLED:
        return redirect("files:index")

    try:
        javascript, stylesheets = _load_spa_entry(str(settings.SPA_MANIFEST_PATH))
    except SPABuildUnavailable:
        logger.exception("The SPA shell could not load its Vite manifest.")
        return HttpResponse(
            "Dotori web assets are unavailable. Rebuild the app image or set "
            "DOTORI_SPA_ENABLED=0 to use the legacy interface.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )

    return render(
        request,
        "spa/index.html",
        {
            "spa_javascript": javascript,
            "spa_stylesheets": stylesheets,
        },
    )
