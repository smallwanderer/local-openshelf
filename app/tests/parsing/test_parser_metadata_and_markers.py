from enum import Enum
from pathlib import Path

import pytest

from document_ai.parsers.hwp_parser import _normalize_extracted_text
from document_ai.parsers.text_utils import (
    _restore_protected_table_entities,
    serialize_meta,
    strip_inline_code_in_table_rows,
)


pytestmark = pytest.mark.unit


class Label(Enum):
    PICTURE = "picture"


def test_serialize_meta_recursively_produces_json_values():
    source = {
        "headings": ("One", "Two"),
        "item": {
            "label": Label.PICTURE,
            "path": Path("/tmp/source.pdf"),
        },
    }

    result = serialize_meta(source)

    assert result == {
        "headings": ["One", "Two"],
        "item": {
            "label": "picture",
            "path": "/tmp/source.pdf",
        },
    }
    assert serialize_meta(result) == result


def test_hwp_standalone_image_marker_is_removed_but_caption_is_kept():
    text = "<그림>\n그림 1. 시스템 전체 구성도\n본문"

    assert _normalize_extracted_text(text) == "그림 1. 시스템 전체 구성도\n본문"


def test_strip_inline_code_in_table_rows_unwraps_whole_cell_code_span():
    text = (
        "| Priority | Meaning |\n"
        "|----------|---------|\n"
        "| `speed` | latency first |\n"
    )

    result = strip_inline_code_in_table_rows(text)

    assert "`" not in result
    assert "| speed | latency first |" in result


def test_strip_inline_code_in_table_rows_unwraps_mixed_text_cell():
    text = "| DEBUG 값 | .env에서 `true`/`false`로 설정 |"

    result = strip_inline_code_in_table_rows(text)

    assert result == "| DEBUG 값 | .env에서 true/false로 설정 |"


def test_strip_inline_code_in_table_rows_escapes_angle_brackets():
    text = "| 파일 | `data/config/<scope>/llm_runtime.json` |"

    result = strip_inline_code_in_table_rows(text)

    assert result == "| 파일 | data/config/&lt;scope&gt;/llm_runtime.json |"


def test_strip_inline_code_in_table_rows_supports_double_backtick_delimiter():
    text = "| 표현 | ``literal ` backtick`` |"

    result = strip_inline_code_in_table_rows(text)

    assert result == "| 표현 | literal &#96; backtick |"


def test_strip_inline_code_in_table_rows_protects_pipe_and_ampersand():
    text = "| 조건 | `A | B & C` |"

    result = strip_inline_code_in_table_rows(text)

    assert result == "| 조건 | A &#124; B &amp; C |"


def test_restore_protected_table_entities_restores_pipe():
    assert _restore_protected_table_entities("A &#124; B") == "A | B"


def test_strip_inline_code_in_table_rows_leaves_unclosed_span_untouched():
    text = "| 설명 | an `unclosed span |"

    assert strip_inline_code_in_table_rows(text) == text


def test_strip_inline_code_in_table_rows_leaves_prose_and_fences_untouched():
    text = (
        "Use `install.py --run` to start the server.\n"
        "\n"
        "```\n"
        "| `not` | `a table` |\n"
        "```\n"
    )

    assert strip_inline_code_in_table_rows(text) == text


def test_strip_inline_code_in_table_rows_tracks_long_and_tilde_fences():
    text = (
        "````markdown\n"
        "| `inside` | ``double`` |\n"
        "````\n"
        "~~~markdown\n"
        "| `also inside` | unchanged |\n"
        "~~~\n"
        "| `outside` | normalized |\n"
    )

    result = strip_inline_code_in_table_rows(text)

    assert "| `inside` | ``double`` |" in result
    assert "| `also inside` | unchanged |" in result
    assert "| outside | normalized |" in result
