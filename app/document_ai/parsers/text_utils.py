from typing import Any, Optional, Dict, List
from enum import Enum
from pathlib import Path
import re

from .constants import TEXT_LIKE_EXTENSIONS, BINARY_DOC_EXTENSIONS, HWP_EXTENSIONS, guess_code_fence_language

class ExtFormat(Enum):
    STRING = "string"
    FILE = "file"
    HWP = "hwp"


def serialize_meta(meta: Any) -> Any:
    if meta is None:
        return None
    if hasattr(meta, "model_dump"):
        return meta.model_dump()
    if hasattr(meta, "dict"):
        return meta.dict()
    if hasattr(meta, "__dict__"):
        return meta.__dict__
    return str(meta)


def detect_input_format(file_path: str) -> ExtFormat:
    ext = Path(file_path).suffix.lower()
    if ext in TEXT_LIKE_EXTENSIONS:
        return ExtFormat.STRING
    elif ext in BINARY_DOC_EXTENSIONS:
        return ExtFormat.FILE
    elif ext in HWP_EXTENSIONS:
        return ExtFormat.HWP
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def read_textfile_with_fallback(file_path: str) -> str:
    encodings = ["utf-8", "utf-8-sig", "cp949", "euc-kr"]

    last_error = None
    for encoding in encodings:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError as e:
            last_error = e

    # Final fallback
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


"""
.txt, .yaml, .json 등 텍스트 기반 파일의 경우, 마크다운(.md)로 변환해주는 함수
YAML, Script 파일의 경우 마크다운의 코드 블록(Code Block) 문법으로 변환하여 문맥 보존
"""
def convert_to_markdown(file_path: str) -> str:
    text = read_textfile_with_fallback(file_path)
    ext = Path(file_path).suffix.lower()
    file_name = Path(file_path).name

    if ext in {".md", ".markdown"}:
        return text

    if ext in {
        ".yaml", ".yml", ".json", ".py", ".sh", ".bash",
        ".sql", ".xml", ".html", ".htm", ".js", ".ts",
        ".toml", ".ini", ".cfg", ".conf"
    }:
        lang = guess_code_fence_language(ext)
        title = f"# File: {file_name}\n\n"
        return f"{title}```{lang}\n{text}\n```"

    # 일반 텍스트
    return f"# Document: {file_name}\n\n{text}"


def wrap_text_as_markdown(text: str, document_name: str) -> str:
    return f"# Document: {Path(document_name).name}\n\n{text}"


def normalize_extracted_text(text: str) -> str:
    if not isinstance(text, str):
        return ""

    normalized = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
    )

    normalized_lines = []
    for line in normalized.split("\n"):
        collapsed = re.sub(r"[ \t\f\v]+", " ", line).strip()
        normalized_lines.append(collapsed)

    normalized = "\n".join(normalized_lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


# ──────────────────────────────────────────────
# 직렬화 보조 함수
# ──────────────────────────────────────────────

def _safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _safe_dict(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None

    if isinstance(value, dict):
        return value

    if hasattr(value, "model_dump"):
        try:
            # mode="json" converts non-serializable objects (like Path) to primitives
            data = value.model_dump(mode="json")
            if isinstance(data, dict):
                return data
            return {"value": data}
        except Exception:
            pass

    if hasattr(value, "dict"):
        try:
            if hasattr(value, "json"):
                import json
                data = json.loads(value.json())
            else:
                data = value.dict()
                
            if isinstance(data, dict):
                return data
            return {"value": data}
        except Exception:
            pass

    if hasattr(value, "__dict__"):
        try:
            return dict(value.__dict__)
        except Exception:
            pass

    try:
        return {"value": str(value)}
    except Exception:
        return None


def _safe_list_of_dict(value: Any) -> Optional[List[Dict[str, Any]]]:
    if value is None:
        return None

    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                result.append(item)
            else:
                result.append(_safe_dict(item) or {"value": str(item)})
        return result

    try:
        return [_safe_dict(item) or {"value": str(item)} for item in value]
    except Exception:
        return None
