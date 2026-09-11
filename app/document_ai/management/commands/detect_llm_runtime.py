from __future__ import annotations

import json
from dataclasses import replace
from django.core.management.base import BaseCommand, CommandError

from document_ai.services.rag_runtime_config import (
    ServerRAGTarget,
    clear_server_rag_target_cache,
    get_llm_runtime_config_path,
)
from llm_installation.catalog import (
    catalog_rows,
    evaluate_catalog_fit,
    get_catalog_entry,
    get_rag_model_catalog,
)
from llm_installation.cli import (
    json_output,
    model_detail_dict,
    print_model_table,
    render_table,
)
from llm_installation.config_store import write_llm_runtime_config, write_runtime_generation
from llm_installation.planner import (
    PRIORITY_PRESETS,
    assess_catalog_entry,
    build_serving_plan,
    convert_policy,
)
from llm_installation.router import resolve_server_rag_target
from llm_installation.runtime_lifecycle import make_generation_id
from llm_installation.selection import (
    SelectionCandidate,
    estimated_decode_tps,
    rank_manual_candidates,
    select_catalog_model,
)
from llm_installation.runtime_handoff import (
    build_runtime_policy_input,
)
from llm_installation.runtime_probe import (
    probe_server_runtime,
)


class Command(BaseCommand):
    help = "Detect the self-hosted server LLM runtime and optionally persist the selected RAG target."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cluster-mode",
            action="store_true",
            help="Resolve compatible models to vLLM for clustered/distributed serving.",
        )
        parser.add_argument(
            "--priority",
            choices=PRIORITY_PRESETS,
            default="balanced",
            help="Optimize automatic model and serving settings for speed, balance, or quality.",
        )
        parser.add_argument(
            "--write",
            action="store_true",
            help="Persist the detected runtime selection to data/config/llm_runtime.json.",
        )
        parser.add_argument(
            "--check-endpoint",
            action="store_true",
            help="Call /health and /v1/models on candidate endpoints before selecting a target.",
        )
        parser.add_argument(
            "--smoke-test",
            action="store_true",
            help="Send a short chat completion request to the selected candidate before accepting it.",
        )
        parser.add_argument(
            "--list-models",
            action="store_true",
            help="Print the model catalog table and exit unless --write is also provided.",
        )
        parser.add_argument(
            "--search",
            default="",
            help="Filter the model catalog table by text.",
        )
        parser.add_argument(
            "--show",
            default="",
            help="Show detailed information for a model id and exit.",
        )
        parser.add_argument(
            "--json-output",
            action="store_true",
            help="Print machine-readable JSON.",
        )
        parser.add_argument(
            "--interactive",
            "-i",
            action="store_true",
            help="Run interactive 11-step installation and LLM management wizard.",
        )

    PAGE_SIZE = 20

    def _candidate_table_row(self, candidate, index):
        entry = candidate.entry
        assessment = candidate.assessment
        placement = assessment.memory_placement
        if placement is None:
            est_mem = "Unavailable"
        else:
            vram_label = ",".join(
                str(value) for value in placement.required_vram_per_gpu_mb
            ) or "-"
            est_mem = f"RAM {placement.required_ram_mb} / VRAM [{vram_label}] MB"
        tps = estimated_decode_tps(assessment)
        est_tps = f"{tps:.1f} tps" if tps is not None else "N/A"
        fit_color = (
            self.style.SUCCESS if assessment.fit_status == "FIT"
            else self.style.WARNING if assessment.fit_status == "RISKY"
            else self.style.ERROR
        )
        row = [str(index), entry.id, assessment.backend_profile or "-", assessment.fit_status, est_mem, est_tps]
        row_styles = [None, None, None, fit_color, None, None]
        return row, row_styles

    def _print_candidate_page(self, candidates, start_index):
        headers = ["Idx", "Model ID", "Candidate Type", "Status", "Est RAM/VRAM", "Est TPS"]
        widths = [5, 28, 18, 8, 32, 10]
        rows = []
        row_styles = []
        for offset, candidate in enumerate(candidates):
            row, styles = self._candidate_table_row(candidate, start_index + offset)
            rows.append(row)
            row_styles.append(styles)
        render_table(headers, widths, rows, self.stdout, row_styles=row_styles)

    def _run_manual_selection(self, display_candidates, priority_preset):
        total = len(display_candidates)
        last_page = max((total - 1) // self.PAGE_SIZE, 0)
        page = 0
        while True:
            start = page * self.PAGE_SIZE
            end = min(start + self.PAGE_SIZE, total)
            self._print_candidate_page(display_candidates[start:end], start + 1)
            self.stdout.write(f"Showing items {start + 1}-{end} of {total}.")
            self.stdout.write("Commands: [Enter/n] Next Page, [p] Previous Page, [Index] Select, [q] Quit")
            choice = input("> ").strip().lower()
            if choice == "q":
                raise CommandError("Manual model selection was cancelled.")
            if choice in ("", "n"):
                if page < last_page:
                    page += 1
                else:
                    self.stdout.write(self.style.WARNING("Already on the last page."))
                continue
            if choice == "p":
                if page > 0:
                    page -= 1
                else:
                    self.stdout.write(self.style.WARNING("Already on the first page."))
                continue
            if not choice.isdigit() or not 1 <= int(choice) <= total:
                self.stdout.write(self.style.ERROR("Please enter a valid catalog index."))
                continue
            selected_artifact_id = display_candidates[int(choice) - 1].entry.id
            selection_result = select_catalog_model(
                display_candidates,
                priority_preset,
                selection_mode="manual",
                selected_artifact_id=selected_artifact_id,
            )
            if selection_result.selection_status == "INVALID_MANUAL_SELECTION":
                self.stdout.write(self.style.ERROR("That model cannot be selected."))
                continue
            return selected_artifact_id

    def handle(self, *args, **options):
        profile = probe_server_runtime()
        if options["cluster_mode"]:
            profile = replace(profile, cluster_mode=True)
        catalog = get_rag_model_catalog()
        rows = catalog_rows(profile, query=options["search"])

        if options["show"]:
            entry = get_catalog_entry(options["show"])
            if entry is None:
                raise CommandError(f"Unknown catalog model id: {options['show']}")
            show_rows = catalog_rows(profile)
            row = next(item for item in show_rows if item["id"] == entry.id)
            if options["json_output"]:
                self.stdout.write(json_output(model_detail_dict(row)))
            else:
                for key, value in model_detail_dict(row).items():
                    self.stdout.write(f"{key}: {value}")
            return

        if options["interactive"]:
            self.stdout.write(self.style.MIGRATE_HEADING("\n=== 1. Server Hardware Diagnostics ==="))
            self.stdout.write(f"CPU Model: {profile.cpu_model or 'Unknown'} ({profile.cpu_count} Cores)")
            self.stdout.write(f"CPU Features: {', '.join(profile.cpu_features or ['None'])}")
            self.stdout.write(f"RAM: Total {profile.ram_mb} MB, Available {profile.ram_available_mb} MB")
            self.stdout.write(f"Disk: Total {profile.disk_total_mb} MB, Free {profile.disk_free_mb} MB")
            self.stdout.write(f"GPU Name: {profile.gpu_name or 'None'} (Count: {profile.gpu_count})")
            self.stdout.write(f"GPU VRAM: Total {profile.gpu_vram_mb} MB, Free {profile.gpu_vram_free_mb} MB")
            self.stdout.write(f"GPU Compute Capability: {profile.gpu_compute_cap or 'N/A'}")
            if profile.gpu_mem_bandwidth_gb_s:
                self.stdout.write(f"GPU Memory Bandwidth: {profile.gpu_mem_bandwidth_gb_s:.1f} GB/s")
            else:
                self.stdout.write("GPU Memory Bandwidth: N/A")
            self.stdout.write(f"CUDA Available: {profile.cuda_available}")
            self.stdout.write(f"Docker Available: {profile.docker_available} (Compose: {profile.docker_compose_available})")
            self.stdout.write("==================================================\n")

            self.stdout.write(self.style.MIGRATE_HEADING("=== 2. Operating Policy Policy Setting ==="))
            self.stdout.write("Please select your desired LLM operating policy:")
            self.stdout.write("1) Speed (Optimized for fastest token generation, lower context)")
            self.stdout.write("2) Balanced (Balance speed, context, and quality. Default)")
            self.stdout.write("3) Quality (Optimized for maximum capability, higher context)")
            
            policy_choice = input("Enter choice (1-3, default: 2): ").strip()
            if policy_choice == "1":
                priority_preset = "speed"
            elif policy_choice == "3":
                priority_preset = "quality"
            else:
                priority_preset = "balanced"

            policy_config = convert_policy(priority_preset)
            self.stdout.write(f"Selected Policy configuration:")
            self.stdout.write(f"- Target Context: {policy_config.target_context} tokens")
            self.stdout.write(f"- Target Concurrency: {policy_config.target_concurrency}")
            self.stdout.write(f"- Memory Policy: {policy_config.memory_policy}")
            self.stdout.write(f"- Engine Preference: {policy_config.engine_preference}")
            self.stdout.write("")

            # Generate and calculate candidate fits
            selection_candidates = []
            for entry in catalog:
                assessment = assess_catalog_entry(entry, profile)
                fit_evaluation = evaluate_catalog_fit(entry, profile)
                selection_candidates.append(SelectionCandidate(entry, assessment, fit_evaluation))

            self.stdout.write("Select model selection method:")
            self.stdout.write("1) Automatic recommendation (default)")
            self.stdout.write("2) Choose from the assessed model catalog")
            selection_choice = input("Enter choice (1-2, default: 1): ").strip()
            selection_mode = "manual" if selection_choice == "2" else "automatic"

            self.stdout.write(self.style.MIGRATE_HEADING("=== 3. Model Ranking & Automatic Selection ==="))

            selected_artifact_id = None
            if selection_mode == "manual":
                ranked, non_selectable = rank_manual_candidates(selection_candidates, priority_preset)
                display_candidates = ranked + non_selectable
                selected_artifact_id = self._run_manual_selection(display_candidates, priority_preset)
                selection_result = select_catalog_model(
                    display_candidates,
                    priority_preset,
                    selection_mode="manual",
                    selected_artifact_id=selected_artifact_id,
                )
            else:
                selection_result = select_catalog_model(
                    selection_candidates,
                    priority_preset,
                    selection_mode=selection_mode,
                )

            if selection_result.selection_status == "RISKY_CONFIRMATION_REQUIRED":
                confirmation = input(
                    "The selected model is RISKY. Continue without safety headroom? [y/N]: "
                ).strip().lower()
                if confirmation not in {"y", "yes"}:
                    raise CommandError("RISKY model selection was not confirmed.")
                selection_result = select_catalog_model(
                    selection_candidates,
                    priority_preset,
                    selection_mode=selection_mode,
                    selected_artifact_id=selected_artifact_id,
                    risky_confirmed=True,
                )
            if selection_result.selection_status != "SELECTED":
                raise CommandError("No selectable FIT or confirmed RISKY model exists.")

            selected_candidate = selection_result.selected_candidate
            selected_entry = selected_candidate.entry
            selected_plan = selected_candidate.assessment
            runtime_policy_input = build_runtime_policy_input(
                selection_result,
                profile,
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Selected {selected_entry.id} using {selection_mode} mode for {priority_preset}."
                )
            )

            selected_assessment = selected_plan
            selected_plan = build_serving_plan(runtime_policy_input)
            # Resolve RAG Target
            base_url = selected_plan.base_url
            if base_url.endswith("/v1"):
                base_url = base_url[:-3].rstrip("/")

            target = ServerRAGTarget(
                endpoint_name=f"{selected_entry.display_name} ({selected_plan.candidate_type})",
                base_url=base_url,
                model=selected_entry.id,
                runtime=selected_plan.runtime,
                reason=selected_assessment.fit_reason,
                fallback_used=False,
                serving_profile=selected_plan.as_dict(),
                diagnostics={"profile": {}, "candidates": []},
                priority_preset=priority_preset,
                selection_mode=selection_mode,
                selection_reason_code=selection_result.reason_code,
                runtime_policy_input=runtime_policy_input.as_dict(),
            )

            # Stage a candidate generation only -- this command never touches
            # Docker (the app container has no Docker CLI/socket). The host
            # installer reads pending_generation.json, builds a RuntimeSpec,
            # and validates+activates it via RuntimeLifecycleManager.apply().
            generation_id = make_generation_id(runtime_policy_input.integrity_sha256)
            generation_dir = write_runtime_generation(
                scope="production",
                target=target,
                profile=profile,
                catalog=catalog,
                generation_id=generation_id,
            )
            pending_path = generation_dir.parent.parent / "pending_generation.json"
            pending_path.write_text(
                json.dumps(
                    {
                        "generation_id": generation_id,
                        "runtime": target.runtime,
                        "model_id": target.model,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.stdout.write(
                self.style.SUCCESS(f"\nSaved candidate runtime generation: {generation_dir}")
            )
            self.stdout.write(
                self.style.WARNING(
                    "Host installer will build, validate, and activate the selected runtime container."
                )
            )
            return

        if options["list_models"] or options["search"]:
            if not options["write"]:
                if options["json_output"]:
                    self.stdout.write(json_output({"models": rows}))
                else:
                    print_model_table(rows, self.stdout)
                return
            if not options["json_output"]:
                print_model_table(rows, self.stdout)

        target = resolve_server_rag_target(
            profile=profile,
            catalog=catalog,
            check_endpoint=options["check_endpoint"],
            smoke_test=options["smoke_test"],
            priority_preset=options["priority"],
        )

        json_payload = {
            "profile": target.diagnostics.get("profile", {}) if target.diagnostics else {},
            "target": {
                "endpoint_name": target.endpoint_name,
                "base_url": target.base_url,
                "model": target.model,
                "runtime": target.runtime,
                "fallback_used": target.fallback_used,
                "reason": target.reason,
                "priority_preset": target.priority_preset,
                "serving_profile": target.serving_profile or {},
            },
            "diagnostics": target.diagnostics or {},
        }
        if options["json_output"] and not options["write"]:
            self.stdout.write(json_output(json_payload))
            return

        if not options["json_output"]:
            self.stdout.write("Detected server runtime")
            self.stdout.write(f"cpu_count: {profile.cpu_count}")
            self.stdout.write(f"ram_mb: {profile.ram_mb}")
            self.stdout.write(f"has_gpu: {profile.has_gpu}")
            self.stdout.write(f"gpu_name: {profile.gpu_name or '-'}")
            self.stdout.write(f"gpu_vram_mb: {profile.gpu_vram_mb}")
            self.stdout.write(f"cuda_available: {profile.cuda_available}")
            self.stdout.write(f"disk_free_mb: {profile.disk_free_mb}")
            self.stdout.write(f"container_memory_limit_mb: {profile.container_memory_limit_mb}")
            self.stdout.write(f"cuda_driver_version: {profile.cuda_driver_version or '-'}")
            self.stdout.write(f"driver_supported_cuda_version: {profile.driver_supported_cuda_version or '-'}")
            self.stdout.write(f"docker_available: {profile.docker_available}")
            self.stdout.write(f"docker_compose_available: {profile.docker_compose_available}")
            self.stdout.write(f"docker_service_count: {len(profile.docker_services or [])}")
            self.stdout.write("")
            self.stdout.write("Selected RAG target")
            self.stdout.write(f"endpoint_name: {target.endpoint_name}")
            self.stdout.write(f"base_url: {target.base_url}")
            self.stdout.write(f"model: {target.model}")
            self.stdout.write(f"runtime: {target.runtime}")
            self.stdout.write(f"fallback_used: {target.fallback_used}")
            self.stdout.write(f"reason: {target.reason}")
            self.stdout.write(f"priority_preset: {target.priority_preset}")
            if target.serving_profile:
                self.stdout.write(f"context_length: {target.serving_profile.get('context_length')}")
                self.stdout.write(
                    "safe_concurrency_ceiling: "
                    f"{target.serving_profile.get('safe_concurrency_ceiling')}"
                )
                self.stdout.write(
                    "serving_concurrency: "
                    f"{target.serving_profile.get('serving_concurrency')}"
                )
                self.stdout.write(
                    "calibration_status: "
                    f"{target.serving_profile.get('calibration_status')}"
                )
                self.stdout.write(
                    f"logical_total_memory_mb: {target.serving_profile.get('logical_total_memory_mb')}"
                )
                self.stdout.write(
                    f"required_ram_mb: {target.serving_profile.get('required_ram_mb')}"
                )
                self.stdout.write(
                    "required_vram_per_gpu_mb: "
                    f"{target.serving_profile.get('required_vram_per_gpu_mb', [])}"
                )
            if target.health_status:
                self.stdout.write(f"health_ok: {target.health_status.ok} ({target.health_status.message})")
            if target.endpoint_status:
                self.stdout.write(f"models_ok: {target.endpoint_status.ok} ({target.endpoint_status.message})")
            if target.smoke_status:
                self.stdout.write(f"smoke_ok: {target.smoke_status.ok} ({target.smoke_status.message})")

            diagnostics = target.diagnostics or {}
            candidates = diagnostics.get("candidates", [])
            if candidates:
                self.stdout.write("")
                self.stdout.write("Candidate diagnostics")
                for candidate in candidates:
                    status = "selected" if candidate.get("selected") else "rejected"
                    reason = candidate.get("rejected_reason") or "-"
                    self.stdout.write(
                        f"- {status}: {candidate.get('runtime')} / {candidate.get('model')} "
                        f"@ {candidate.get('base_url')} reason={reason}"
                    )

        if not options["write"]:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run only. Re-run with --write to persist {get_llm_runtime_config_path()}."
                )
            )
            return

        config_path = write_llm_runtime_config(
            target=target,
            profile=profile,
            catalog=catalog,
        )
        clear_server_rag_target_cache()
        if options["json_output"]:
            json_payload["config_path"] = str(config_path)
            self.stdout.write(json_output(json_payload))
            return
        if not options["json_output"]:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"Saved LLM runtime config: {config_path}"))
