import json
import subprocess

import pytest

from llm_installation.runtime_probe import probe_docker_services

pytestmark = pytest.mark.unit


def test_probe_docker_services_decodes_docker_output_as_utf8(monkeypatch):
    payload = [
        {
            "Name": "dotori-app",
            "Service": "app",
            "State": "running",
            "Status": "Up 1 minute • healthy",
        }
    ]

    def fake_run(args, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_docker_services() == [
        {
            "name": "dotori-app",
            "service": "app",
            "state": "running",
            "status": "Up 1 minute • healthy",
        }
    ]


def test_probe_docker_services_handles_missing_stdout(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=None, stderr=None
        ),
    )

    assert probe_docker_services() == []
