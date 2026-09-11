from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 파서 공통 결과 스키마 (특정 backend에 종속되지 않는 DTO)
#
# 위쪽 필드들은 "어떤 backend든 이 정도는 채울 수 있다"는 공통 계약이다
# (chunks/status/errors/parser_backend 등 -- 대부분 다른 파서도 자연스럽게
# 낼 수 있는 값). backend_metadata는 그 반대로, 특정 backend 내부 구현에만
# 의미 있는 부가 정보를 담는 자유 형식 bag이다 -- 예를 들어 docling backend는
# 여기에 docling_core_version/pages/confidence 같은 자신의 ConversionResult
# 필드를 담는다. 다른 backend는 이 bag에 아무것도 안 넣어도 되고, 자기만의
# 키를 넣어도 된다. 다운스트림(save_parse_result 등)은 이 bag의 내용을
# 읽지 않는다 -- 순수 디버그/재현성 정보다.
# ──────────────────────────────────────────────

class ChunkPayload(BaseModel):
    """단일 청크. 어떤 backend가 만들었든 이 모양이어야 한다."""
    chunk_index: int
    serialized_text: str
    tokens: int
    meta: Optional[Dict[str, Any]] = None


class ParseResult(BaseModel):
    """파서 backend 공통 출력 DTO"""
    parser_mode: str
    parser_backend: str
    file_path: str
    file_ext: str
    chunks: List[ChunkPayload]

    status: Optional[str] = None
    parser_version: Optional[str] = None

    # input 계열 (어떤 backend든 낼 수 있는 일반적인 문서 식별 정보)
    input_format: Optional[str] = None
    input_document_hash: Optional[str] = None
    input_page_count: Optional[int] = None
    page_count: Optional[int] = None

    # 실행 결과 계열
    errors: Optional[List[Dict[str, Any]]] = None
    timings: Optional[Dict[str, Any]] = None

    # backend 전용 부가 정보 (예: docling의 버전/페이지/confidence 등).
    # 다운스트림 파이프라인은 이 안을 들여다보지 않는다.
    backend_metadata: Dict[str, Any] = Field(default_factory=dict)
