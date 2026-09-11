import json
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from document_ai.search.hnsw_benchmark import (
    DEFAULT_EF_SEARCH_VALUES,
    DEFAULT_K_VALUES,
    parse_positive_int_list,
    run_hnsw_benchmark,
    write_benchmark_result,
)
from document_ai.models import EmbeddingGeneration
from workspaces.models import Workspace


def _resolve_workspace(value: str) -> Workspace:
    if value.isdigit():
        workspace = Workspace.objects.filter(pk=int(value)).first()
    else:
        try:
            workspace_uid = UUID(value)
        except ValueError as exc:
            raise CommandError("--workspace must be a numeric ID or UUID.") from exc
        workspace = Workspace.objects.filter(uid=workspace_uid).first()
    if workspace is None:
        raise CommandError(f"Workspace {value!r} was not found.")
    return workspace


class Command(BaseCommand):
    help = "Measure workspace-scoped pgvector HNSW Recall@K, EXPLAIN ANALYZE, and latency."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True, help="Workspace numeric ID or UUID.")
        parser.add_argument("--generation", help="Embedding generation ID; required if several exist.")
        parser.add_argument("--sample-size", type=int, default=25)
        parser.add_argument("--k", default=",".join(map(str, DEFAULT_K_VALUES)))
        parser.add_argument(
            "--ef-search",
            default=",".join(map(str, DEFAULT_EF_SEARCH_VALUES)),
        )
        parser.add_argument("--repeats", type=int, default=3)
        parser.add_argument("--seed", type=int, default=20260902)
        parser.add_argument("--min-recall", type=float, default=0.95)
        parser.add_argument("--output-dir", default="/data/evaluation/runs")

    def handle(self, *args, **options):
        workspace = _resolve_workspace(options["workspace"])
        try:
            k_values = parse_positive_int_list(options["k"], option_name="--k")
            ef_search_values = parse_positive_int_list(
                options["ef_search"], option_name="--ef-search"
            )
            result = run_hnsw_benchmark(
                workspace=workspace,
                generation_id=options.get("generation"),
                sample_size=options["sample_size"],
                k_values=k_values,
                ef_search_values=ef_search_values,
                repeats=options["repeats"],
                seed=options["seed"],
                min_recall=options["min_recall"],
            )
            output_path = write_benchmark_result(result, Path(options["output_dir"]))
        except (ValueError, EmbeddingGeneration.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc

        payload = {
            "output_path": str(output_path),
            "scope": result["scope"],
            "parameters": result["parameters"],
            "summary": result["summary"],
            "plan_facts": {
                "default": result["plans"]["default"]["facts"],
                "exact": result["plans"]["exact"]["facts"],
                "ann": {
                    key: value["facts"] for key, value in result["plans"]["ann"].items()
                },
            },
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
