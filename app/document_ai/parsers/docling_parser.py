import typing
import os
from pathlib import Path
from typing import List

from docling.document_converter import InputFormat
from docling_core.transforms.chunker.hierarchical_chunker import (
    ChunkingDocSerializer,
    ChunkingSerializerProvider,
)
from docling_core.transforms.serializer.markdown import (
    MarkdownTableSerializer,
    MarkdownParams,
)

from .config import get_hf_tokenizer, get_converter, get_hybrid_hf_chunker
from .schema import ChunkPayload, ParseResult
from .text_utils import (
    ExtFormat,
    serialize_meta,
    normalize_extracted_text,
    convert_to_markdown,
    wrap_text_as_markdown,
    detect_input_format,
    strip_inline_code_in_table_rows,
    _restore_protected_table_entities,
    _safe_str,
    _safe_int,
    _safe_dict,
    _safe_list_of_dict,
)
from .hwp_parser import convert_hwp_to_txt, convert_hwpx_to_markdown

__all__ = [
    "ChunkPayload",
    "ParseResult",
    "parse_document",
    "parse_document_string",
    "parse_document_hwp",
    "parse_document_entry",
]

# ──────────────────────────────────────────────
# 테이블을 마크다운 형식으로 직렬화하기 위한 시리얼라이저 프로바이더
# 이미지 placeholder를 검색 텍스트에 출력하지 않는 시리얼라이저 프로바이더
# ──────────────────────────────────────────────

class CustomSerializerProvider(ChunkingSerializerProvider):
    def get_serializer(self, doc):
        return ChunkingDocSerializer(
            doc=doc,
            table_serializer=MarkdownTableSerializer(),
            params=MarkdownParams(
                image_placeholder="",
            ),
        )

# ──────────────────────────────────────────────
# 핵심 파싱 로직
# ──────────────────────────────────────────────

def _parse_docling_document(
    result: typing.Any,  # docling.datamodel.document.ConversionResult
    file_path: str,
    parser_mode: str,
) -> ParseResult:

    tokenizer = get_hf_tokenizer()
    chunker = get_hybrid_hf_chunker(
        serializer_provider=CustomSerializerProvider()
    )

    # Chunker
    chunk_iter = chunker.chunk(result.document)

    chunks: List[ChunkPayload] = []
    for chunk in chunk_iter:
        serialized_text = normalize_extracted_text(
            _restore_protected_table_entities(
                chunker.contextualize(chunk=chunk)
            )
        )
        if not serialized_text:
            continue
        meta = serialize_meta(chunk.meta) if hasattr(chunk, "meta") else None

        chunks.append(
            ChunkPayload(
                chunk_index=len(chunks),
                serialized_text=serialized_text,
                tokens=tokenizer.count_tokens(serialized_text),
                meta=meta,
            )
        )

    input_obj = getattr(result, "input", None)
    version_obj = getattr(result, "version", None)
    document_obj = getattr(result, "document", None)
    pages_obj = getattr(result, "pages", None)

    return ParseResult(
        parser_mode=parser_mode,
        parser_backend="docling",
        file_path=file_path,
        file_ext=Path(file_path).suffix.lower(),
        chunks=chunks,

        status=_safe_str(getattr(result, "status", None)),
        parser_version=_safe_str(getattr(version_obj, "docling_version", None)) if version_obj else None,

        # input 계열 (다른 backend도 낼 수 있는 일반적인 문서 식별 정보)
        input_format=_safe_str(getattr(input_obj, "format", None)) if input_obj else None,
        input_document_hash=_safe_str(getattr(input_obj, "document_hash", None)) if input_obj else None,
        input_page_count=_safe_int(getattr(input_obj, "page_count", None)) if input_obj else None,
        page_count=len(pages_obj) if pages_obj is not None else None,

        # 실행 결과 계열
        errors=_safe_list_of_dict(getattr(result, "errors", None)),
        timings=_safe_dict(getattr(result, "timings", None)),

        # docling ConversionResult 전용 부가 정보. 다른 backend는 이 bag을
        # 안 채워도 되고, 자기만의 키를 채워도 된다.
        backend_metadata={
            "timestamp": _safe_str(getattr(result, "timestamp", None)),
            "input_info": _safe_dict(input_obj),
            "input_file": _safe_str(getattr(input_obj, "file", None)) if input_obj else None,
            "input_filesize": _safe_int(getattr(input_obj, "filesize", None)) if input_obj else None,
            "version_info": _safe_dict(version_obj),
            "docling_core_version": _safe_str(getattr(version_obj, "docling_core_version", None)) if version_obj else None,
            "docling_ibm_models_version": _safe_str(getattr(version_obj, "docling_ibm_models_version", None)) if version_obj else None,
            "docling_parse_version": _safe_str(getattr(version_obj, "docling_parse_version", None)) if version_obj else None,
            "platform_str": _safe_str(getattr(version_obj, "platform_str", None)) if version_obj else None,
            "py_impl_version": _safe_str(getattr(version_obj, "py_impl_version", None)) if version_obj else None,
            "py_lang_version": _safe_str(getattr(version_obj, "py_lang_version", None)) if version_obj else None,
            "pages": _safe_list_of_dict(pages_obj),
            "document": _safe_dict(document_obj),
            "confidence": _safe_dict(getattr(result, "confidence", None)),
            "assembled": _safe_dict(getattr(result, "assembled", None)),
        },
    )


def parse_document(file_path: str) -> ParseResult:
    converter = get_converter()
    # result: docling.datamodel.document.ConversionResult
    result = converter.convert(file_path)

    return _parse_docling_document(
        result=result,
        file_path=file_path,
        parser_mode="convert",
    )


"""
document가 Markdown이거나, HTML인 경우, convert_string을 사용해야 함
"""
def parse_document_string(file_path: str) -> ParseResult:
    md_content = strip_inline_code_in_table_rows(convert_to_markdown(file_path))
    converter = get_converter()

    result = converter.convert_string(
        content=md_content,
        format=InputFormat.MD,
        name=os.path.basename(file_path),
    )

    return _parse_docling_document(
        result=result,
        file_path=file_path,
        parser_mode="convert_string_md",
    )

"""
document가 아래아한글인 경우, parse_document_string을 사용
"""
def parse_document_hwp(file_path: str) -> ParseResult:
    ext = Path(file_path).suffix.lower()
    if ext == ".hwp":
        string_document = convert_hwp_to_txt(file_path)
        md_content = wrap_text_as_markdown(string_document, file_path)
    elif ext == ".hwpx":
        md_content = convert_hwpx_to_markdown(file_path)
    else:
        raise ValueError(f"Unsupported hwp format: {file_path}")

    md_content = strip_inline_code_in_table_rows(md_content)
    converter  = get_converter()
    
    result = converter.convert_string(
        content=md_content,
        format=InputFormat.MD,
        name=os.path.basename(file_path),
    )
    return _parse_docling_document(
        result=result,
        file_path=file_path,
        parser_mode="convert_string_md",
    )


def parse_document_entry(file_path: str) -> ParseResult:
    """파일 확장자에 따라 적절한 파싱 방식을 선택하여 실행"""
    mode = detect_input_format(file_path)

    if mode == ExtFormat.HWP:
        return parse_document_hwp(file_path)
    elif mode == ExtFormat.STRING:
        return parse_document_string(file_path)
    return parse_document(file_path)
