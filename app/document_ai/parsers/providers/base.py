from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from document_ai.parsers.schema import ParseResult

__all__ = ["DocumentParserSpec", "DocumentParser", "ParseResult"]


@dataclass(frozen=True)
class DocumentParserSpec:
    backend: str
    # 이 backend가 직접 처리할 수 있다고 선언하는 확장자 집합. detect_input_format()이
    # 판단하는 "이 확장자가 어떤 경로(STRING/FILE/HWP)로 가야 하는가"와는 다른 축이다 --
    # 이건 "이 backend가 그 경로를 실제로 구현했는가"를 나타낸다.
    supported_extensions: frozenset[str]


class DocumentParser(Protocol):
    spec: DocumentParserSpec

    def parse(self, file_path: str) -> ParseResult:
        ...

    def healthcheck(self) -> dict:
        ...
