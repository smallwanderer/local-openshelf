# STATUS

Date: 2026-04-09  
Base branch/commit: `main` / `1f228a0`

## 1. Current Status

### 1.1 Implemented
- Account features
  - Custom user model with email-based login
  - Signup, login, logout, email verification, verification resend
  - Email verification guard via decorator
- File management
  - Tree structure with `Node` and `FileBlob`
  - Upload validation, save, download
  - Rename, move, star, trash (restore, permanent delete, empty)
  - Separated page views and JSON API (`page_views`, `api_v1/file_views`)
- Document AI
  - Parsing/chunk/embedding models (`DocumentParseResult`, `DocumentChunk`, `ChunkEmbedding`)
  - Upload-triggered parse via Django signal
  - Async pipeline with Celery (`parse -> embed`)
  - Vector search API (`/api/document-ai/v1/search/`)
- Infrastructure
  - Docker Compose stack (Django, Postgres+pgvector, Redis, Celery, Nginx)
  - Swagger and Redoc endpoints
- Tests
  - Unit tests for file model/service
  - Docling DTO unit/integration test code

### 1.2 Not Implemented / Needs Fix
- Unimplemented functions in `app/files/services/storage.py` (`pass`)
  - `delete_file`, `open_file`, `get_download_response`, `get_file`, `get_files`
- Missing template for verification-required page
  - `accounts/verification_required.html`
- AI search link identifier mismatch risk
  - Search response uses integer `node_id` while file detail route expects UUID
- Test suite cleanup needed
  - `app/tests/temp.py` and `app/tests/verify_rag_api.py` are manual scripts, not stable automated tests
- Text/template encoding cleanup needed
  - Existing mojibake-like strings should be normalized to UTF-8 source text

### 1.3 Risks
- Production configuration separation is incomplete
  - `SECRET_KEY` hardcoded, `DEBUG=True` default
- Runtime artifact control is incomplete
  - Local cache/output paths (for example `app/.cache`) need stricter ignore policy
- Embedding workload may hit memory limits
  - No batching/resource throttling strategy yet

## 2. Roadmap

### 2.1 Short Term (1-2 weeks): Stabilization
- Add missing verification-required template
- Fix UUID alignment for AI search result links
- Normalize broken text/strings and template encoding to UTF-8
- Implement or remove currently unused `pass` functions
- Tighten `.gitignore` for cache/temp/runtime artifacts

### 2.2 Mid Term (2-4 weeks): Quality
- Add E2E coverage for upload -> parse -> embed -> retrieve
- Improve Celery observability for retry/failure states
- Standardize API response and error schema
- Split automated tests and manual verification scripts clearly

### 2.3 Long Term (1-2 months): Scale & Operations
- Optimize embedding pipeline
  - Batch embedding and queue/resource policies
- Improve retrieval quality
  - Threshold/top_k tuning and prompt context refinement
- Improve production readiness
  - Environment-specific settings (dev/stage/prod), security hardening
  - Backup/recovery and monitoring/alerting

## 3. Next Action Proposal

- Priority 1: stabilization patch set (missing template, UUID mismatch, encoding cleanup)
- Priority 2: automated testing baseline (E2E + regression)
- Priority 3: production/security/resource optimization
