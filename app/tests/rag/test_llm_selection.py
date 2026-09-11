from types import SimpleNamespace

import pytest

from llm_installation.selection import (
    SelectionCandidate,
    select_catalog_model,
)

pytestmark = pytest.mark.unit


def _candidate(
    artifact_id,
    *,
    fit_status="FIT",
    tps=10.0,
    priority=0,
    parameters=7.0,
    quant="q4_k_m",
    selectable=True,
    auto_selectable=None,
    manual_selectable=None,
):
    if auto_selectable is None:
        auto_selectable = selectable
    if manual_selectable is None:
        manual_selectable = selectable
    entry = SimpleNamespace(
        id=artifact_id,
        priority=priority,
        artifact=SimpleNamespace(quant=quant, dtype=None, format="gguf"),
        model_metadata=SimpleNamespace(
            parameter_count_b=parameters,
            max_context_length=4096,
        ),
    )
    assessment = SimpleNamespace(estimated_decode_tps=tps)
    fit_evaluation = SimpleNamespace(
        fit_status=fit_status,
        eligibility=SimpleNamespace(
            auto_selectable=auto_selectable,
            manual_selectable=manual_selectable,
        ),
        ram_check=SimpleNamespace(required_mb=100, available_mb=200),
        vram_checks=[],
        disk_check=SimpleNamespace(required_mb=100, available_mb=200),
    )
    return SelectionCandidate(entry, assessment, fit_evaluation)


def test_fit_always_outranks_risky():
    result = select_catalog_model(
        [
            _candidate("risky-fast", fit_status="RISKY", tps=100),
            _candidate("fit-slow", fit_status="FIT", tps=1),
        ],
        "speed",
        risky_confirmed=True,
    )

    assert result.selected_candidate.entry.id == "fit-slow"
    assert result.reason_code == "SELECTED_FIT_BY_SPEED"


def test_risky_requires_explicit_confirmation():
    candidate = _candidate("risky", fit_status="RISKY")

    pending = select_catalog_model([candidate], "balanced")
    selected = select_catalog_model(
        [candidate],
        "balanced",
        risky_confirmed=True,
    )

    assert pending.selection_status == "RISKY_CONFIRMATION_REQUIRED"
    assert pending.selected_candidate is None
    assert selected.selection_status == "SELECTED"
    assert selected.selected_candidate.entry.id == "risky"


def test_speed_uses_tps_and_artifact_id_is_final_tie_break():
    result = select_catalog_model(
        [
            _candidate("z-slow", tps=5),
            _candidate("b-fast", tps=20),
            _candidate("a-fast", tps=20),
        ],
        "speed",
    )

    assert result.ranked_artifact_ids == ["a-fast", "b-fast", "z-slow"]


def test_nofit_and_ineligible_candidates_are_never_selected():
    result = select_catalog_model(
        [
            _candidate("nofit", fit_status="NOFIT"),
            _candidate("hidden", selectable=False),
        ],
        "quality",
    )

    assert result.selection_status == "NO_SELECTABLE_MODEL"
    assert result.ranked_artifact_ids == []


def test_manual_selection_uses_requested_fit_candidate():
    result = select_catalog_model(
        [_candidate("first", tps=100), _candidate("chosen", tps=1)],
        "speed",
        selection_mode="manual",
        selected_artifact_id="chosen",
    )

    assert result.selection_status == "SELECTED"
    assert result.selected_candidate.entry.id == "chosen"
    assert result.reason_code == "SELECTED_MANUAL_FIT"


def test_manual_risky_selection_requires_confirmation():
    candidate = _candidate("risky", fit_status="RISKY")
    pending = select_catalog_model(
        [candidate],
        "quality",
        selection_mode="manual",
        selected_artifact_id="risky",
    )
    selected = select_catalog_model(
        [candidate],
        "quality",
        selection_mode="manual",
        selected_artifact_id="risky",
        risky_confirmed=True,
    )

    assert pending.selection_status == "RISKY_CONFIRMATION_REQUIRED"
    assert selected.reason_code == "SELECTED_MANUAL_RISKY"


def test_manual_selection_rejects_nofit_candidate():
    result = select_catalog_model(
        [_candidate("nofit", fit_status="NOFIT")],
        "balanced",
        selection_mode="manual",
        selected_artifact_id="nofit",
    )

    assert result.selection_status == "INVALID_MANUAL_SELECTION"


def test_manual_only_candidate_is_not_automatically_selected():
    candidate = _candidate(
        "manual-only",
        auto_selectable=False,
        manual_selectable=True,
    )

    automatic = select_catalog_model([candidate], "balanced")
    manual = select_catalog_model(
        [candidate],
        "balanced",
        selection_mode="manual",
        selected_artifact_id="manual-only",
    )

    assert automatic.selection_status == "NO_SELECTABLE_MODEL"
    assert manual.selection_status == "SELECTED"
