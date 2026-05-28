from __future__ import annotations

QUERY_DSL_SCHEMA = {
    "node": {
        "model": "files.Node",
        "prefix": "",
        "fields": {
            "uid": {"type": "uuid", "operators": {"eq", "in"}},
            "name": {"type": "str", "operators": {"eq", "neq", "contains", "in"}},
            "ext": {"type": "str", "operators": {"eq", "neq", "in"}},
            "node_type": {"type": "str", "operators": {"eq", "neq", "in"}},
            "description": {"type": "str", "operators": {"contains"}},
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
    "user": {
        "model": "accounts.User",
        "prefix": "owner__",
        "fields": {
            "email": {"type": "str", "operators": {"eq", "contains"}},
        },
        "sortable_fields": {"email"},
    },
}

QUERY_DSL_SCHEMA_NOTUSING_NOW = {
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
        "prefix": "parse_result__chunks__embeddings__",
        "fields": {},
        "sortable_fields": set(),
    },
    "user_storage": {
        "model": "files.UserStorage",
        "prefix": "owner__storage__",
        "fields": {},
        "sortable_fields": set(),
    },
}