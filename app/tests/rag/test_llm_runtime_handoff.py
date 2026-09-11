from types import SimpleNamespace

import pytest

from llm_installation.runtime_handoff import (
    RuntimeHandoffError,
    build_runtime_policy_input,
)
from llm_installation.selection import (
    SelectionCandidate,
    SelectionResult,
)

pytestmark = pytest.mark.unit


def _selection(*, fit_status="FIT", risky_confirmed=False):
    entry = SimpleNamespace(
        id="artifact-a",
        model_id="model-a",
        backend_profiles=["llamacpp-cpu"],
    )
    assessment = SimpleNamespace(
        runtime="llama.cpp",
        backend_profile="llamacpp-cpu",
        fit_status=fit_status,
        memory_placement={"required_ram_mb": 1024},
    )
    fit_evaluation = SimpleNamespace(fit_status=fit_status)
    candidate = SelectionCandidate(entry, assessment, fit_evaluation)
    return SelectionResult(
        selection_status="SELECTED",
        selection_mode="manual",
        priority_preset="balanced",
        selected_candidate=candidate,
        ranked_artifact_ids=["artifact-a"],
        reason_code=(
            "SELECTED_MANUAL_RISKY"
            if fit_status == "RISKY"
            else "SELECTED_MANUAL_FIT"
        ),
        risky_confirmed=risky_confirmed,
    )


def test_handoff_freezes_selected_assessment_and_hardware():
    selection = _selection()
    profile = SimpleNamespace(cpu_count=8, ram_mb=32768)

    handoff = build_runtime_policy_input(selection, profile)
    profile.ram_mb = 1
    selection.selected_candidate.assessment.runtime = "vllm"

    assert handoff.runtime == "llama.cpp"
    assert handoff.hardware_profile["ram_mb"] == 32768
    assert handoff.catalog_assessment["runtime"] == "llama.cpp"
    assert handoff.verify_integrity() is True


def test_handoff_rejects_unconfirmed_risky_selection():
    with pytest.raises(RuntimeHandoffError, match="not confirmed"):
        build_runtime_policy_input(
            _selection(fit_status="RISKY", risky_confirmed=False),
            SimpleNamespace(cpu_count=8),
        )


def test_handoff_rejects_backend_not_declared_by_artifact():
    selection = _selection()
    selection.selected_candidate.assessment.backend_profile = "vllm-cuda"
    selection.selected_candidate.assessment.runtime = "vllm"

    with pytest.raises(RuntimeHandoffError, match="not declared"):
        build_runtime_policy_input(selection, SimpleNamespace(cpu_count=8))


def test_handoff_detects_changed_payload():
    handoff = build_runtime_policy_input(
        _selection(),
        SimpleNamespace(cpu_count=8),
    )
    object.__setattr__(handoff, "runtime", "vllm")

    assert handoff.verify_integrity() is False


def test_handoff_rejects_fit_status_mismatch():
    selection = _selection()
    selection.selected_candidate.assessment.fit_status = "RISKY"

    with pytest.raises(RuntimeHandoffError, match="status disagree"):
        build_runtime_policy_input(selection, SimpleNamespace(cpu_count=8))
