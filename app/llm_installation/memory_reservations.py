"""Cross-workload memory claims for a runtime scope.

Dotori runs two independently registered model workloads on one machine: the
LLM serving container and the embedding executor. Each is sized at a different
moment, so neither one's own registration can see what the other already holds
-- an LLM planned while the embedding model happens to be unloaded will claim
memory that disappears the moment a document is ingested.

This module is the single place where "what is currently claimed on this
machine" lives, so a later sizing decision can subtract what an earlier one
already took.

It deliberately does NOT live in either runtime's own config file:

* ``embedding_runtime.json`` hashes its whole payload into
  ``runtime_fingerprint``, and that fingerprint is stored per document in
  ``DocumentParseResult.embedding_runtime_fingerprint`` and used to filter
  search results. Adding a field there would change every fingerprint and drop
  the entire corpus out of search.
* The LLM generation's ``runtime.json`` records what the planner computed when
  that generation was staged. That is a historical plan, not a live claim: it
  stays on disk after the runtime is removed. The claim here is copied from it
  at activation and released on removal.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1

CLAIM_LLM = "llm"
CLAIM_EMBEDDING = "embedding"
KNOWN_WORKLOADS = (CLAIM_LLM, CLAIM_EMBEDDING)

# A claim is released only by an explicit removal, never by the runtime merely
# being stopped: a stopped-but-configured runtime is expected to start again,
# and if the other workload grew into its memory meanwhile that restart fails.
# Over-reserving briefly is cheaper than a runtime that cannot come back.
STATE_ACTIVE = "active"

SOURCE_PLANNER = "planner"
SOURCE_DECLARED = "declared"
SOURCE_MEASURED = "measured"

DEVICE_CPU = "cpu"
DEVICE_CUDA = "cuda"
# A workload served by someone else's hardware (an external embedding endpoint)
# claims nothing locally, but still records a claim so the absence is explicit
# rather than indistinguishable from "never registered".
DEVICE_REMOTE = "remote"


@dataclass(frozen=True)
class MemoryClaim:
    """One workload's current hold on this machine's memory pools."""

    workload: str
    generation_id: str
    device: str
    ram_mb: int = 0
    vram_per_gpu_mb: tuple[int, ...] = ()
    components: dict[str, int] = field(default_factory=dict)
    bounds: dict[str, int] = field(default_factory=dict)
    source: str = SOURCE_DECLARED
    state: str = STATE_ACTIVE
    claimed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "generation_id": self.generation_id,
            "device": self.device,
            "ram_mb": int(self.ram_mb),
            "vram_per_gpu_mb": [int(value) for value in self.vram_per_gpu_mb],
            "components": {key: int(value) for key, value in self.components.items()},
            "bounds": {key: int(value) for key, value in self.bounds.items()},
            "source": self.source,
            "claimed_at": self.claimed_at,
        }

    @classmethod
    def from_dict(cls, workload: str, payload: Any) -> "MemoryClaim | None":
        if not isinstance(payload, dict):
            return None
        return cls(
            workload=workload,
            generation_id=str(payload.get("generation_id") or ""),
            device=str(payload.get("device") or DEVICE_CPU),
            ram_mb=_non_negative_int(payload.get("ram_mb")),
            vram_per_gpu_mb=tuple(
                _non_negative_int(value)
                for value in (payload.get("vram_per_gpu_mb") or [])
            ),
            components=_int_mapping(payload.get("components")),
            bounds=_int_mapping(payload.get("bounds")),
            source=str(payload.get("source") or SOURCE_DECLARED),
            state=str(payload.get("state") or STATE_ACTIVE),
            claimed_at=str(payload.get("claimed_at") or ""),
        )


@dataclass(frozen=True)
class ReservationTotal:
    """The sum of every active claim, per memory pool.

    Never stored: a persisted total can drift out of step with the parts it
    came from, and then nothing on disk says which of the two is right.
    """

    ram_mb: int
    vram_per_gpu_mb: tuple[int, ...]
    workloads: tuple[str, ...]

    def vram_for_gpu(self, index: int) -> int:
        if 0 <= index < len(self.vram_per_gpu_mb):
            return self.vram_per_gpu_mb[index]
        return 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ram_mb": self.ram_mb,
            "vram_per_gpu_mb": list(self.vram_per_gpu_mb),
            "workloads": list(self.workloads),
        }


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _non_negative_int(item) for key, item in value.items()}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_memory_reservations_path(
    scope: str | None = None, *, repo_root: Path | None = None
) -> Path:
    resolved_scope = scope or os.getenv("RUNTIME_SCOPE", "production")
    root = repo_root or Path(__file__).resolve().parents[2]
    return (
        root
        / "data"
        / "config"
        / "runtime_scopes"
        / resolved_scope
        / "memory_reservations.json"
    )


def read_reservations(
    scope: str | None = None, *, repo_root: Path | None = None
) -> dict[str, MemoryClaim]:
    """Return every active claim for the scope, keyed by workload.

    A missing or unreadable file means "nothing is claimed" rather than an
    error: this is consulted on sizing paths that must still work on a fresh
    install, and an unreadable file must not be able to block registration.
    """
    path = get_memory_reservations_path(scope, repo_root=repo_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, dict):
        return {}

    claims: dict[str, MemoryClaim] = {}
    for workload, claim_payload in raw_claims.items():
        claim = MemoryClaim.from_dict(str(workload), claim_payload)
        if claim is not None and claim.state == STATE_ACTIVE:
            claims[claim.workload] = claim
    return claims


def _write_reservations(
    claims: dict[str, MemoryClaim],
    *,
    scope: str,
    repo_root: Path | None = None,
) -> Path:
    path = get_memory_reservations_path(scope, repo_root=repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "updated_at": _utc_now(),
        "claims": {
            workload: claim.as_dict() for workload, claim in sorted(claims.items())
        },
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def write_claim(
    claim: MemoryClaim, *, scope: str, repo_root: Path | None = None
) -> Path:
    """Record one workload's claim, leaving the other workloads' claims intact."""
    claims = read_reservations(scope, repo_root=repo_root)
    if not claim.claimed_at:
        claim = replace(claim, claimed_at=_utc_now())
    claims[claim.workload] = claim
    return _write_reservations(claims, scope=scope, repo_root=repo_root)


def clear_claim(
    workload: str, *, scope: str, repo_root: Path | None = None
) -> Path | None:
    """Release one workload's claim. Called on explicit removal only."""
    claims = read_reservations(scope, repo_root=repo_root)
    if workload not in claims:
        return None
    del claims[workload]
    return _write_reservations(claims, scope=scope, repo_root=repo_root)


def _llm_generation_runtime_path(
    scope: str, generation_id: str, repo_root: Path | None = None
) -> Path:
    root = repo_root or Path(__file__).resolve().parents[2]
    return (
        root
        / "data"
        / "config"
        / "runtime_scopes"
        / scope
        / "generations"
        / generation_id
        / "runtime.json"
    )


def build_llm_claim(
    scope: str, generation_id: str, *, repo_root: Path | None = None
) -> MemoryClaim | None:
    """Derive the LLM claim from what the planner already recorded.

    Nothing is recomputed here: ``serving_profile.memory_placement`` is the
    planner's own output for this generation, so the claim cannot disagree with
    the plan the runtime was actually started with.
    """
    path = _llm_generation_runtime_path(scope, generation_id, repo_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    target = payload.get("target")
    if not isinstance(target, dict):
        return None
    serving_profile = target.get("serving_profile")
    if not isinstance(serving_profile, dict):
        return None
    placement = serving_profile.get("memory_placement")
    if not isinstance(placement, dict):
        # Older generations carry the totals on the serving profile itself.
        placement = serving_profile

    ram_mb = _non_negative_int(placement.get("required_ram_mb"))
    vram_per_gpu = tuple(
        _non_negative_int(value)
        for value in (placement.get("required_vram_per_gpu_mb") or [])
    )
    components = _int_mapping(placement.get("ram_components_mb"))
    for per_gpu in placement.get("vram_components_per_gpu_mb") or []:
        for key, value in _int_mapping(per_gpu).items():
            components[key] = components.get(key, 0) + value

    return MemoryClaim(
        workload=CLAIM_LLM,
        generation_id=generation_id,
        device=DEVICE_CUDA if any(vram_per_gpu) else DEVICE_CPU,
        ram_mb=ram_mb,
        vram_per_gpu_mb=vram_per_gpu,
        components=components,
        source=SOURCE_PLANNER,
    )


def claim_llm_runtime(
    scope: str, generation_id: str, *, repo_root: Path | None = None
) -> MemoryClaim | None:
    """Record the LLM claim for an activated generation, if one can be derived.

    Best effort on purpose: a runtime that just passed health validation must
    not be reported as failed because its claim could not be written.
    """
    claim = build_llm_claim(scope, generation_id, repo_root=repo_root)
    if claim is None:
        return None
    try:
        write_claim(claim, scope=scope, repo_root=repo_root)
    except OSError:
        return None
    return claim


def get_total_reservation(
    scope: str | None = None,
    *,
    exclude: str | None = None,
    repo_root: Path | None = None,
) -> ReservationTotal:
    """Sum the active claims per pool.

    ``exclude`` drops one workload from the sum, which is what a workload needs
    when re-planning itself: it must subtract everyone else's hold, not its own
    previous one.
    """
    claims = read_reservations(scope, repo_root=repo_root)
    if exclude:
        claims.pop(exclude, None)

    ram_mb = 0
    vram: list[int] = []
    for claim in claims.values():
        ram_mb += claim.ram_mb
        for index, value in enumerate(claim.vram_per_gpu_mb):
            if index < len(vram):
                vram[index] += value
            else:
                vram.append(value)
    return ReservationTotal(
        ram_mb=ram_mb,
        vram_per_gpu_mb=tuple(vram),
        workloads=tuple(sorted(claims)),
    )
