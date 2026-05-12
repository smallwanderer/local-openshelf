import typing
import os
from pathlib import Path
from enum import Enum
from typing import Optional, Dict, Any, List
from pydantic import BaseModel

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
from .text_utils import (
    ExtFormat,
    serialize_meta,
    normalize_extracted_text,
    convert_to_markdown,
    wrap_text_as_markdown,
    detect_input_format,
    _safe_str,
    _safe_int,
    _safe_dict,
    _safe_list_of_dict,
)
from .hwp_parser import convert_hwp_to_txt, convert_hwpx_to_markdown


# ──────────────────────────────────────────────
# Pydantic 결과 스키마 (파서 출력 전용)
# ──────────────────────────────────────────────

class ChunkPayload(BaseModel):
    """docling chunker가 생산한 단일 청크"""
    chunk_index: int
    serialized_text: str
    tokens: int
    meta: Optional[Dict[str, Any]] = None


class ParseResult(BaseModel):
    """docling 파싱 전체 결과 : WIde DTO"""
    parser_mode: str
    file_path: str
    file_ext: str
    chunks: List[ChunkPayload]

    # ConversionResult 기본 메타
    status: Optional[str] = None
    timestamp: Optional[str] = None

    # input 계열
    input_info: Optional[Dict[str, Any]] = None
    input_format: Optional[str] = None
    input_file: Optional[str] = None
    input_filesize: Optional[int] = None
    input_page_count: Optional[int] = None
    input_document_hash: Optional[str] = None

    # version 계열
    version_info: Optional[Dict[str, Any]] = None
    parser_version: Optional[str] = None
    docling_core_version: Optional[str] = None
    docling_ibm_models_version: Optional[str] = None
    docling_parse_version: Optional[str] = None
    platform_str: Optional[str] = None
    py_impl_version: Optional[str] = None
    py_lang_version: Optional[str] = None

    # document / pages 계열
    detected_language: Optional[str] = None
    page_count: Optional[int] = None
    pages: Optional[List[Dict[str, Any]]] = None
    document: Optional[Dict[str, Any]] = None

    # 실행 결과 계열
    errors: Optional[List[Dict[str, Any]]] = None
    timings: Optional[Dict[str, Any]] = None
    confidence: Optional[Dict[str, Any]] = None
    assembled: Optional[Dict[str, Any]] = None

# ──────────────────────────────────────────────
# 테이블을 마크다운 형식으로 직렬화하기 위한 시리얼라이저 프로바이더
# 이미지를 "<!-- image -->"로 치환하기 위한 시리얼라이저 프로바이더
# ──────────────────────────────────────────────

class CustomSerializerProvider(ChunkingSerializerProvider):
    def get_serializer(self, doc):
        return ChunkingDocSerializer(
            doc=doc,
            table_serializer=MarkdownTableSerializer(),
            params=MarkdownParams(
                image_placeholder="<!-- image -->",
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
    for i, chunk in enumerate(chunk_iter):
        serialized_text = normalize_extracted_text(
            chunker.contextualize(chunk=chunk)
        )
        meta = serialize_meta(chunk.meta) if hasattr(chunk, "meta") else None

        chunks.append(
            ChunkPayload(
                chunk_index=i,
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
        file_path=file_path,
        file_ext=Path(file_path).suffix.lower(),
        chunks=chunks,

        # ConversionResult 기본 메타
        status=_safe_str(getattr(result, "status", None)),
        timestamp=_safe_str(getattr(result, "timestamp", None)),

        # input 계열
        input_info=_safe_dict(input_obj),
        input_format=_safe_str(getattr(input_obj, "format", None)) if input_obj else None,
        input_file=_safe_str(getattr(input_obj, "file", None)) if input_obj else None,
        input_filesize=_safe_int(getattr(input_obj, "filesize", None)) if input_obj else None,
        input_page_count=_safe_int(getattr(input_obj, "page_count", None)) if input_obj else None,
        input_document_hash=_safe_str(getattr(input_obj, "document_hash", None)) if input_obj else None,

        # version 계열
        version_info=_safe_dict(version_obj),
        parser_version=_safe_str(getattr(version_obj, "docling_version", None)) if version_obj else None,
        docling_core_version=_safe_str(getattr(version_obj, "docling_core_version", None)) if version_obj else None,
        docling_ibm_models_version=_safe_str(getattr(version_obj, "docling_ibm_models_version", None)) if version_obj else None,
        docling_parse_version=_safe_str(getattr(version_obj, "docling_parse_version", None)) if version_obj else None,
        platform_str=_safe_str(getattr(version_obj, "platform_str", None)) if version_obj else None,
        py_impl_version=_safe_str(getattr(version_obj, "py_impl_version", None)) if version_obj else None,
        py_lang_version=_safe_str(getattr(version_obj, "py_lang_version", None)) if version_obj else None,

        # document / pages 계열
        page_count=len(pages_obj) if pages_obj is not None else None,
        pages=_safe_list_of_dict(pages_obj),
        document=_safe_dict(document_obj),

        # 실행 결과 계열
        errors=_safe_list_of_dict(getattr(result, "errors", None)),
        timings=_safe_dict(getattr(result, "timings", None)),
        confidence=_safe_dict(getattr(result, "confidence", None)),
        assembled=_safe_dict(getattr(result, "assembled", None)),
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
    md_content = convert_to_markdown(file_path)
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
