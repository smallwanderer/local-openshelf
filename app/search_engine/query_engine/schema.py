from __future__ import annotations

QUERY_DSL_SCHEMA = {
    "node": {
        "model": "files.Node",
        "prefix": "",
        "fields": {
            "name": {"type": "str", "operators": {"eq", "neq", "contains", "in"}},
            "ext": {"type": "str", "operators": {"eq", "neq", "in"}},
            "path": {"type": "str", "operators": {"eq", "contains"}},
            "starred": {"type": "bool", "operators": {"eq", "neq"}},
            "created_at": {"type": "datetime", "operators": {"eq", "gte", "lte", "gt", "lt"}},
            "updated_at": {"type": "datetime", "operators": {"eq", "gte", "lte", "gt", "lt"}},
        },
        "sortable_fields": {"name", "ext", "created_at", "updated_at"},
    },
    "fileblob": {
        "model": "files.FileBlob",
        "prefix": "blob__",
        "fields": {
            "size": {"type": "int", "operators": {"eq", "gte", "lte", "gt", "lt"}},
            "mime_type": {"type": "str", "operators": {"eq", "contains", "in"}},
        },
        "sortable_fields": {"size"},
    },
}

QUERY_DSL_SCHEMA_NOTUSING_NOW = {
    "user": {
        "model": "accounts.User",
        "prefix": "owner__",
        "fields": {
            "email": {"type": "str", "operators": {"eq", "contains"}},
        },
        "sortable_fields": {"email"},
    },
    "parse_result": {
        "model": "document_ai.DocumentParseResult",
        "prefix": "parse_result__",
        "fields": {},
        "sortable_fields": set(),
    },
    "chunk": {
        "model": "document_ai.DocumentChunk",
        "prefix": "parse_result__chunks__",
        "fields": {},
        "sortable_fields": set(),
    },
    "embedding": {
        "model": "document_ai.ChunkEmbedding",
        "prefix": "parse_result__chunks__embedding__",
        "fields": {},
        "sortable_fields": set(),
    },
    "user_storage": {
        "model": "files.UserStorage",
        "prefix": "workspace__storage__",
        "fields": {},
        "sortable_fields": set(),
    },
}
