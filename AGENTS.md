# Repository Guidelines

## Project Structure & Module Organization
`app/` contains the Django project and application code. Core settings and entrypoints live in `app/config/`. Feature apps are split into `app/files/`, `app/accounts/`, and `app/document_ai/`; keep views, services, models, and templates inside the owning app. Shared templates live in `app/templates/`. Tests are mainly under `app/tests/`, with app-local coverage such as `app/files/tests.py`. Runtime data and uploads are mounted through `data/`, and reverse-proxy config lives in `nginx/`.

## Build, Test, and Development Commands
Use Docker for the default production-like stack:

- `docker compose up --build`: start Gunicorn/Django, Postgres with pgvector, Redis, Celery workers, Celery beat, LLM parser, and Nginx.
- `docker compose exec app python manage.py migrate`: apply database migrations.

Use `docker compose -f docker-compose.dev.yml ...` for the development stack:

- `docker compose -f docker-compose.dev.yml up --build`: start the development stack on `http://localhost:8888/`.
- `docker compose -f docker-compose.dev.yml exec app python manage.py migrate`: apply development DB migrations.
- `docker compose -f docker-compose.dev.yml exec app pytest`: run the test suite.
- `docker compose -f docker-compose.dev.yml logs -f app`: inspect web logs.
- `docker compose -f docker-compose.dev.yml logs -f celery-worker`: inspect parse/embed worker logs.
- `docker compose -f docker-compose.dev.yml logs -f celery-search-worker`: inspect AI search worker logs.
- `docker compose -f docker-compose.dev.yml logs -f celery-beat`: inspect recovery scheduler logs.

## Runtime Architecture Notes
The web `app` container should stay lightweight and must not run heavy AI work inline. Upload, download, file listing, auth, and task enqueueing live in `app`. Heavy document work runs through Celery:

- `celery-worker`: `parse,embed` queues for Docling parsing, chunking, and BGE-M3 embedding.
- `celery-search-worker`: `search` queue for query embedding and pgvector retrieval through `SearchJob`.
- `celery-text2sql-worker`: `text2sql` queue.
- `celery-beat`: backlog recovery task scheduling.

The project currently uses Redis as the Celery broker and for recovery/semaphore primitives. RabbitMQ is a future option, but broker migration should be planned separately from Redis lock/semaphore usage.

## Dependency Layout
Dependencies are split by container role:

- `app/requirements.web.txt`: lightweight web dependencies.
- `app/requirements.ai.txt`: Docling/Torch/FlagEmbedding/HWP parser dependencies for AI workers.
- `app/requirements.dev.txt`: development/test dependencies.
- `app/requirements.txt`: aggregate local install file.

Docker build args:

- `INSTALL_AI_DEPS=0`: web app, beat, non-AI workers.
- `INSTALL_AI_DEPS=1`: parse/embed/search workers.
- `INSTALL_DEV_DEPS=1`: development compose.

## Coding Style & Naming Conventions
Follow the existing Python style: 4-space indentation, `snake_case` for functions and modules, `PascalCase` for Django models/forms, and concise docstrings only where behavior is not obvious. Keep business logic in `services/`, task modules, or parser/search modules rather than large view functions. Match naming patterns such as `page_views.py`, `file_service.py`, and Django-generated migration names. Avoid unrelated reformatting.

## Testing Guidelines
Pytest is the active test runner, with Django test setup in `app/conftest.py`. Add new tests under `app/tests/` or next to the app they cover, and use `test_*.py` filenames plus `test_*` function names. Prefer small unit tests for parsers/services, then add integration coverage for request, file-processing, Celery task, and search job flows when behavior crosses app boundaries.

## Commit & Pull Request Guidelines
Recent commits use short, plain subject lines without prefixes, sometimes in Korean. Keep commit subjects brief, imperative, and focused on one change. For pull requests, include: a short problem statement, approach summary, migration/env var notes, test commands run, and screenshots for template/UI changes.

## Security & Configuration Tips
Do not commit real secrets in `.env`. Database, Redis, Celery, media, and AI settings are environment-driven in `app/config/settings.py`; document new variables in the PR. Treat `data/uploads/`, `data/pgdata/`, `data/logs/`, and generated parser output as runtime artifacts, not source files.
