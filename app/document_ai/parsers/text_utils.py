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
    if isinstance(meta, (str, int, float, bool)):
        return meta
    if isinstance(meta, Enum):
        return serialize_meta(meta.value)
    if isinstance(meta, Path):
        return str(meta)
    if hasattr(meta, "model_dump"):
        return serialize_meta(meta.model_dump(mode="json"))
    if hasattr(meta, "dict"):
        return serialize_meta(meta.dict())
    if isinstance(meta, dict):
        return {
            str(key): serialize_meta(value)
            for key, value in meta.items()
        }
    if isinstance(meta, (list, tuple, set)):
        return [serialize_meta(value) for value in meta]
    if hasattr(meta, "__dict__"):
        return serialize_meta(
            {
                key: value
                for key, value in vars(meta).items()
                if not key.startswith("_")
            }
        )
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


_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _fence_run(line: str) -> Optional[tuple[str, int, str]]:
    """Return a Markdown fence character, run length, and trailing text.

    CommonMark allows up to three leading spaces and requires at least three
    matching backticks or tildes. A backtick fence's info string cannot itself
    contain a backtick.
    """
    leading_spaces = len(line) - len(line.lstrip(" "))
    if leading_spaces > 3:
        return None

    stripped = line[leading_spaces:]
    if not stripped or stripped[0] not in {"`", "~"}:
        return None

    fence_char = stripped[0]
    run_length = 0
    while run_length < len(stripped) and stripped[run_length] == fence_char:
        run_length += 1
    if run_length < 3:
        return None

    trailing = stripped[run_length:]
    if fence_char == "`" and "`" in trailing:
        return None
    return fence_char, run_length, trailing


def _is_escaped_backtick(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _normalize_code_span_content(content: str) -> str:
    normalized = content.replace("\n", " ")
    if (
        len(normalized) >= 2
        and normalized.startswith(" ")
        and normalized.endswith(" ")
        and normalized.strip(" ")
    ):
        normalized = normalized[1:-1]
    return normalized


def _escape_inline_code_content(content: str) -> str:
    """Protect code text from Docling's Markdown table parser.

    Backticks are encoded so a literal shorter backtick run inside a longer
    code-span delimiter cannot be interpreted as another CodeSpan. Pipes use
    an HTML entity so unwrapping a span cannot introduce a new table-cell
    boundary. Docling leaves that entity serialized, so
    ``normalize_extracted_text()`` restores it after chunk contextualization.
    """
    return (
        _normalize_code_span_content(content)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("`", "&#96;")
    )


def _restore_protected_table_entities(text: str) -> str:
    """Restore entities that Dotori introduced only for Docling table input."""
    if not isinstance(text, str):
        return ""
    return text.replace("&#124;", "|")


def _unwrap_inline_code_spans(text: str) -> str:
    """Unwrap valid Markdown code spans while preserving their literal text."""
    parts: List[str] = []
    cursor = 0

    while cursor < len(text):
        if text[cursor] != "`" or _is_escaped_backtick(text, cursor):
            parts.append(text[cursor])
            cursor += 1
            continue

        opener_start = cursor
        while cursor < len(text) and text[cursor] == "`":
            cursor += 1
        delimiter_length = cursor - opener_start

        search_at = cursor
        closer_start = None
        closer_end = None
        while search_at < len(text):
            candidate = text.find("`", search_at)
            if candidate < 0:
                break
            if _is_escaped_backtick(text, candidate):
                search_at = candidate + 1
                continue

            candidate_end = candidate
            while candidate_end < len(text) and text[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - candidate == delimiter_length:
                closer_start = candidate
                closer_end = candidate_end
                break
            search_at = candidate_end

        if closer_start is None or closer_end is None:
            parts.append(text[opener_start:cursor])
            continue

        parts.append(_escape_inline_code_content(text[cursor:closer_start]))
        cursor = closer_end

    return "".join(parts)


def strip_inline_code_in_table_rows(markdown_text: str) -> str:
    """docling's markdown table parser promotes any inline code span
    (`` `text` ``) inside a table cell out of the table entirely, leaving an
    empty cell behind and splitting the remaining columns into orphan
    single-column tables. There's no markdown table syntax that keeps a code
    span inline in a way docling respects, so this strips backticks from
    table rows only -- HTML-escaping `<`/`>` in the unwrapped text so it
    isn't swallowed as an HTML tag -- before the markdown reaches docling.
    Inline code in prose and inside fenced code blocks is left untouched. Code
    spans using longer delimiters are supported, including a double-backtick
    span that contains a literal single backtick.

    Root cause (docling/backend/md_backend.py, marko AST walk): the
    `RawText` handler checks `self.in_table` and appends to the open table
    buffer when inside one. The `CodeSpan` handler (backtick spans) does not
    check that flag at all -- it unconditionally calls `self._close_table()`
    and emits the span as a standalone `doc.add_code(...)` block. Since
    `_close_table()` resets `in_table = False`, every backtick inside a
    table row force-flushes the table at that point; a row with N code
    spans is split into N+1 orphan table fragments with columns shifted out
    of alignment. This is a gap in docling's CodeSpan handling, not
    something fixable from a serializer or chunker config on our side, so
    the workaround has to happen before docling ever sees the text.
    """
    lines = markdown_text.split("\n")
    active_fence: Optional[tuple[str, int]] = None
    for index, line in enumerate(lines):
        fence = _fence_run(line)
        if active_fence is None and fence is not None:
            fence_char, fence_length, _ = fence
            active_fence = (fence_char, fence_length)
            continue
        if active_fence is not None:
            if fence is not None:
                fence_char, fence_length, trailing = fence
                if (
                    fence_char == active_fence[0]
                    and fence_length >= active_fence[1]
                    and not trailing.strip()
                ):
                    active_fence = None
            continue
        if "`" not in line or not _TABLE_ROW_RE.match(line):
            continue
        lines[index] = _unwrap_inline_code_spans(line)
    return "\n".join(lines)


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
