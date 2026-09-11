from __future__ import annotations

from document_ai.parsers import docling_parser
from document_ai.parsers.constants import (
    BINARY_DOC_EXTENSIONS,
    HWP_EXTENSIONS,
    TEXT_LIKE_EXTENSIONS,
)
from document_ai.parsers.schema import ParseResult

from .base import DocumentParserSpec

__all__ = ["DoclingParser"]


class DoclingParser:
    """기존 docling_parser.py 모듈 함수를 DocumentParser 계약으로 감싼 provider.

    파싱 로직 자체는 옮기지 않는다 -- docling_parser.py는 여러 테스트가 직접
    import하거나 `unittest.mock.patch("document_ai.parsers.docling_parser.
    parse_document_entry", ...)` 형태로 patch하는 안정된 표면이라, 여기서는
    얇은 어댑터만 추가한다. 모듈을 통째로 import해 매 호출마다
    `docling_parser.parse_document_entry`로 속성 조회하는 이유도 이 patch가
    계속 통하게 하기 위해서다 (`from ... import parse_document_entry`로
    바인딩하면 patch 이후에도 이 모듈은 원래 함수 객체를 계속 참조한다).
    """

    backend = "docling"

    def __init__(self) -> None:
        self.spec = DocumentParserSpec(
            backend=self.backend,
            supported_extensions=frozenset(
                TEXT_LIKE_EXTENSIONS | BINARY_DOC_EXTENSIONS | HWP_EXTENSIONS
            ),
        )

    def parse(self, file_path: str) -> ParseResult:
        return docling_parser.parse_document_entry(file_path)

    def healthcheck(self) -> dict:
        from document_ai.parsers.config import get_hf_tokenizer, get_parser_tokenizer_id

        try:
            get_hf_tokenizer()
        except Exception as exc:
            return {"backend": self.backend, "status": "error", "message": str(exc)}
        return {
            "backend": self.backend,
            "status": "ok",
            "tokenizer": get_parser_tokenizer_id(),
        }
