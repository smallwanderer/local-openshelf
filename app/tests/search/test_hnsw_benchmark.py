import pytest

from document_ai.search.hnsw_benchmark import (
    _plan_facts,
    _set_planner_mode,
    latency_summary,
    parse_positive_int_list,
    recall_at_k,
)

pytestmark = pytest.mark.unit


def test_parse_positive_int_list_deduplicates_and_sorts():
    assert parse_positive_int_list("10, 3,10,1", option_name="--k") == [1, 3, 10]


@pytest.mark.parametrize("value", ["", "0", "1,-2", "one"])
def test_parse_positive_int_list_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_positive_int_list(value, option_name="--k")


def test_recall_at_k_uses_exact_prefix_as_denominator():
    assert recall_at_k([10, 20, 30], [10, 99, 30], 3) == pytest.approx(2 / 3)
    assert recall_at_k([10], [10, 20], 3) == 1.0


def test_latency_summary_uses_nearest_rank_tail_percentiles():
    summary = latency_summary([1, 2, 3, 4, 100])

    assert summary == {
        "samples": 5,
        "mean": 22.0,
        "p50": 3.0,
        "p95": 100.0,
        "p99": 100.0,
    }


def test_ann_planner_mode_disables_explicit_sort_to_force_ordered_hnsw_path():
    class RecordingCursor:
        def __init__(self):
            self.settings = {}

        def execute(self, sql, params):
            self.settings[params[0]] = params[1]

    cursor = RecordingCursor()

    _set_planner_mode(cursor, mode="ann", ef_search=20)

    assert cursor.settings["enable_seqscan"] == "off"
    assert cursor.settings["enable_sort"] == "off"
    assert cursor.settings["enable_indexscan"] == "on"
    assert cursor.settings["hnsw.ef_search"] == "20"
    assert cursor.settings["hnsw.iterative_scan"] == "strict_order"


def test_plan_facts_collects_nested_hnsw_index():
    explain = {
        "Planning Time": 0.1,
        "Execution Time": 0.2,
        "Plan": {
            "Node Type": "Limit",
            "Actual Rows": 10,
            "Plans": [
                {
                    "Node Type": "Index Scan",
                    "Index Name": "chunk_embedding_vector_hnsw_idx",
                }
            ],
        },
    }

    facts = _plan_facts(explain)

    assert facts["node_types"] == ["Limit", "Index Scan"]
    assert facts["index_names"] == ["chunk_embedding_vector_hnsw_idx"]
    assert facts["actual_rows"] == 10
