from pathlib import PurePosixPath

import pytest
from django.contrib.staticfiles.storage import staticfiles_storage
from django.template.loader import render_to_string


pytestmark = pytest.mark.unit


def test_file_list_static_tags_reference_manifest_files(monkeypatch):
    resolved_paths: list[str] = []

    def resolve_static(path: str) -> str:
        resolved_paths.append(path)
        return f"/static/{path}"

    monkeypatch.setattr(staticfiles_storage, "url", resolve_static)

    render_to_string("files/file_list.html", {})

    assert resolved_paths
    assert all(PurePosixPath(path).suffix for path in resolved_paths)
    assert "assets/status/uploaded.svg" in resolved_paths
    assert "assets/icons/folder.svg" in resolved_paths
