import os
import sys

# app/ 디렉토리를 sys.path에 추가 (tests/ 하위에서 실행해도 config나 document_ai를 찾을 수 있도록)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pathlib import Path

from document_ai.parsers.docling_parser import parse_document_entry, ParseResult

pytestmark = pytest.mark.integration


def test_parse_document_entry_with_real_markdown_file(tmp_path: Path):
    """
    실제 markdown 파일을 생성하고,
    parse_document_entry()를 통해 ParseResult가 정상 생성되는지 확인한다.
    """

    sample_file = tmp_path / "sample.md"
    sample_file.write_text(
        "# 제목\n\n"
        "이 문서는 docling parser 통합 테스트용 문서입니다.\n\n"
        "## 섹션 1\n"
        "여기에 본문 내용이 들어갑니다.\n\n"
        "- 항목 1\n"
        "- 항목 2\n",
        encoding="utf-8",
    )

    result = parse_document_entry(str(sample_file))

    assert isinstance(result, ParseResult)

    # 기본 정보
    assert result.file_path == str(sample_file)
    assert result.file_ext == ".md"
    assert result.parser_mode in ("convert_string_md", "convert")

    # chunks
    assert isinstance(result.chunks, list)
    assert len(result.chunks) > 0

    first_chunk = result.chunks[0]
    assert first_chunk.chunk_index >= 0
    assert isinstance(first_chunk.serialized_text, str)
    assert first_chunk.serialized_text.strip() != ""
    assert isinstance(first_chunk.tokens, int)
    assert first_chunk.tokens > 0

    # ConversionResult 기반 추가 정보
    # 값이 항상 채워진다고 단정하지 말고, 타입 위주로 확인
    assert result.status is None or isinstance(result.status, str)
    assert result.timestamp is None or isinstance(result.timestamp, str)

    assert result.input_info is None or isinstance(result.input_info, dict)
    assert result.version_info is None or isinstance(result.version_info, dict)
    assert result.errors is None or isinstance(result.errors, list)
    assert result.timings is None or isinstance(result.timings, dict)

    # 출력 결과 눈으로 볼 수 있도록 프린트 (pytest -s 로 실행할 때 표시됨)
    print("\n\n======== [ 파싱 결과 예시 ] ========")
    print(result.model_dump_json(indent=2))
    print("==================================\n")


def test_parse_document_entry_with_file(tmp_path: Path):    
    project_root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sample_file = project_root / "tests" / "test_files" / "hwpx_test.hwpx"
    
    if not sample_file.exists():
        pytest.skip(f"Test file not found: {sample_file}")

    result = parse_document_entry(str(sample_file))

    assert isinstance(result, ParseResult)
    assert result.file_ext == ".hwpx"
    assert result.parser_mode == "convert_string_md"
    
    # chunks
    assert isinstance(result.chunks, list)
    assert len(result.chunks) > 0

    first_chunk = result.chunks[0]
    assert first_chunk.chunk_index >= 0
    assert isinstance(first_chunk.serialized_text, str)
    assert first_chunk.serialized_text.strip() != ""
    assert isinstance(first_chunk.tokens, int)
    assert first_chunk.tokens > 0

    # print("\n\n======== [ 파싱 결과 예시 (.hwpx) ] ========")
    # print(result.model_dump_json(indent=2))
    # print("==================================\n")


if __name__ == "__main__":
    pytest.main([__file__, "-s", "-k", "test_parse_document_entry_with_file"])
