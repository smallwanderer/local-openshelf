from __future__ import annotations

import importlib

from .config import get_parser_backend
from .providers import DocumentParser

# (모듈 경로, 클래스명) 문자열만 담는다 -- 클래스 자체를 여기서 import하면
# registry 모듈을 import하는 순간 모든 backend의 무거운 의존성(docling -> torch
# 등)이 전부 로드된다. 실제 import는 get_document_parser()가 선택된 backend
# 하나에 대해서만, 호출 시점에 수행한다.
_PARSER_FACTORIES: dict[str, tuple[str, str]] = {
    "docling": ("document_ai.parsers.providers.docling", "DoclingParser"),
}


def get_document_parser(backend: str | None = None) -> DocumentParser:
    resolved_backend = backend or get_parser_backend()

    try:
        module_path, class_name = _PARSER_FACTORIES[resolved_backend]
    except KeyError as exc:
        raise ValueError(f"Unsupported document parser backend: {resolved_backend}") from exc

    module = importlib.import_module(module_path)
    factory = getattr(module, class_name)
    return factory()


def registered_parser_backends() -> list[str]:
    return sorted(_PARSER_FACTORIES)
