# STATUS

Date: 2026-05-11  
Base branch/commit: `main` / `1f228a0`

## 1. Current Status

### 1.1 Implemented
- Runtime stabilization
  - Fixed missing `UserStorage` imports that could cause 500s in upload/storage endpoints
  - Added the missing verification-required template: `accounts/verification_required.html`
  - Applied email-verification guard consistently across file JSON APIs
- File management
  - Implemented the previously empty helpers in `app/files/services/storage.py`
  - Trash behavior now propagates through folder subtrees
  - Trashed files are excluded from document search results
  - Trash retention is now 7 days, with restore-window metadata exposed to the UI
- Document AI
  - Retrieval no longer imports the embedding backend when no embeddings exist
  - Fixed `.hwp` parsing failure caused by passing extracted text into a path-based markdown converter
  - Added file-level AI status summary so parse/embedding progress can be shown in file detail and file list screens
  - Added periodic backlog recovery for parse/chunk/embed gaps via Celery beat
- Settings and environment
  - `settings.py` now reads repo-root `.env` values for Django, DB, media, and email settings
  - `.env.example` was updated to reflect actual runtime configuration
  - Host-side pytest DB access was aligned to Docker port mapping in `app/conftest.py`
- Tests and tooling
  - `pytest.ini` now collects both `app/tests` and `app/files`
  - Added tests for storage helpers, AI status display, trash retention, HWP parsing, retriever behavior, and recovery logic
  - Disabled pytest cache provider to avoid `.pytest_cache` permission noise in this environment
  - Cleaned `requests` dependency compatibility warnings
  - Added `torchvision` so `docling` requirements are satisfied and `pip check` passes
- UI and text cleanup
  - Rewrote account templates and account-side messages that had broken text
  - Cleaned key user-facing file/trash/detail messages that were still malformed

### 1.2 Implemented But Still Needs Follow-up
- Periodic recovery exists, but it is currently DB-state/staleness based
  - It does not inspect whether the same work is already sitting in the Celery queue
  - It does not check worker or queue load before requeueing recovery work
- Embedding recovery path needs one more correction
  - The recovery task resets stale chunks to `PENDING`
  - It then queues `embedding_document_with_bge`
  - That task currently expects `PROCESSING`, so recovered embedding work can be skipped unless this path is revised
- Recovery retry policy is still basic
  - There is no persistent retry-attempt counter
  - Permanently bad files can be retried repeatedly on the stale interval

### 1.3 Remaining Fixes / Cleanup
- Encoding cleanup is not finished across the whole repo
  - Some file templates, comments, and legacy strings still need normalization
- Manual vs automated test boundaries still need cleanup
  - There are still manual/debug-oriented files mixed into `app/tests`
- Recovery observability is still thin
  - Recovery actions are not yet surfaced with strong per-file audit metadata
- End-to-end validation is still limited
  - Core unit/regression coverage improved substantially, but full upload -> parse -> embed -> retrieve -> recover E2E coverage is still missing

### 1.4 Risks
- Recovery can requeue duplicate work
  - Because queue contents are not checked, stale DB state can cause duplicate re-enqueue attempts
- Recovery can add pressure even when embed workers are busy
  - The current scheduler uses stale thresholds and batch limits, not queue-idle awareness
- Production defaults still need hardening review
  - Settings are now env-driven, but deployment-specific defaults and secret handling should still be reviewed before production rollout

## 2. Roadmap

### 2.1 Short Term: Recovery Hardening
- Fix embedding recovery so requeued chunks actually enter the correct execution path
- Add duplicate-work protection for recovery scheduling
- Add queue/load-aware throttling before recovery enqueues more parse/embed work
- Add retry-attempt and last-recovery metadata per file/chunk

### 2.2 Mid Term: Quality and Visibility
- Finish repo-wide encoding/text normalization
- Add stronger recovery logging and admin/debug visibility
- Separate automated tests from manual verification scripts more cleanly
- Add E2E coverage for upload -> parse -> chunk -> embed -> retrieve -> trash/restore -> recovery

### 2.3 Long Term: Operations
- Improve embedding throughput and queue policy
  - batching, throttling, and failure isolation
- Improve retrieval quality
  - threshold/top_k tuning and better result context shaping
- Harden production operations
  - environment separation, security review, monitoring, and backup/recovery procedures

## 3. Recommended Next Actions

- Priority 1: fix the embedding-recovery execution path and duplicate requeue risk
- Priority 2: add queue/load-aware recovery throttling and retry metadata
- Priority 3: finish remaining text/encoding cleanup and broaden E2E coverage
