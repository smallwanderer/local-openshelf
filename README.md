# OpenShelf

**OpenShelf is a lightweight full-stack document retriever and RAG system for personal and small-team use.**

It combines file storage, document parsing, hybrid vector retrieval, and retrieval-augmented generation in a Docker Compose stack that can run on a server or a Windows machine with Docker Desktop.

Korean documentation is available in [README.ko.md](README.ko.md). A step-by-step setup guide is available in [WALKTHROUGH.md](WALKTHROUGH.md).

## What It Does

OpenShelf helps you keep documents in a private web workspace and ask questions over them.

- Upload and manage files and folders through a Django web UI.
- Parse documents asynchronously with Celery workers.
- Store document embeddings in PostgreSQL with pgvector.
- Search documents with dense/sparse hybrid retrieval.
- Ask RAG questions and receive answers with citations.
- Run the full system locally or on a small server using Docker Compose.

## Current Release

`0.1.0-alpha` is a pre-release. The core RAG and retrieval workflow is usable, but some advanced features remain experimental.

This release is source-based. Docker images are not published yet. Deploy by checking out the release tag and building with Docker Compose.

## Architecture

```text
nginx
  -> app
      Django web UI, file APIs, task enqueueing

redis
  -> Celery broker

db
  PostgreSQL + pgvector

celery-core-worker
  document parsing, chunking, embedding

celery-search-worker
  query embedding, hybrid retrieval, search jobs

celery-llm-rag-worker
  RAG answer generation

celery-query-worker
  experimental query parsing

celery-text2sql-worker
  experimental Text2SQL tasks

llm-parser
  llama.cpp-compatible local LLM endpoint
```

The web container stays lightweight. Heavy parsing, embedding, retrieval, and LLM work runs in worker containers.

## Main Features

- File and folder management with authenticated access.
- Asynchronous document AI pipeline.
- BGE-M3 based dense/sparse hybrid retrieval.
- RAG question workspace with citation display.
- Embedding-based contextual compression for search and RAG evidence.
- Local desktop sync API groundwork for Shelf-Sync.
- Django admin extensions for operational visibility.
- Experimental QueryDSL parser path with validation and fallback behavior.

## Requirements

- Docker Engine or Docker Desktop
- Docker Compose v2
- Git
- Hugging Face token if the configured LLM or embedding model requires authentication

For Windows, Docker Desktop with WSL2 backend is recommended.

## Quick Start

```bash
git clone https://github.com/smallwanderer/local-openshelf.git
cd local-openshelf
cp .env.example .env
docker compose up -d --build
docker compose exec app python manage.py createsuperuser
```

Open:

```text
http://localhost/
```

Admin:

```text
http://localhost/admin/
```

For development on Windows or local machines:

```bash
cp .env.example .env.dev
docker compose -f docker-compose.dev.yml up -d --build
```

Open:

```text
http://localhost:8888/
```

See [WALKTHROUGH.md](WALKTHROUGH.md) for server and Windows-specific setup steps.

## Important Configuration

Most runtime settings are controlled through `.env` or `.env.dev`.

Key settings:

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key. Change for real deployments. |
| `DJANGO_ALLOWED_HOSTS` | Allowed hostnames or IPs. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Trusted browser origins. |
| `POSTGRES_*` | PostgreSQL credentials. |
| `HF_TOKEN` | Hugging Face token for model downloads when required. |
| `EMBEDDING_MODEL` | Embedding model, currently BGE-M3 by default. |
| `EMBEDDING_DISTANCE_STRATEGY` | Dense retrieval distance strategy. |
| `EMBEDDING_HYBRID_DENSE_WEIGHT` | Dense score weight. |
| `EMBEDDING_HYBRID_SPARSE_WEIGHT` | Sparse score weight. |
| `CONTEXTUAL_COMPRESSION_ENABLED` | Enables evidence compression. |
| `RAG_SEARCH_TOP_K` | Default number of documents searched for RAG. |
| `RAG_RETRIEVAL_THRESHOLD` | RAG dense similarity threshold. |
| `RAG_EVIDENCE_LIMIT` | Maximum citations sent to the RAG prompt. |
| `QUERY_FRONTEND_MODE` | Query parser mode. Default is passthrough. |

## Testing

Run the CI-style unit test command:

```bash
docker compose -f docker-compose.dev.yml run --rm celery-core-worker python -m pytest -m "unit"
```

Run broader development tests from the dev app container:

```bash
docker compose -f docker-compose.dev.yml exec app python -m pytest
```

## Operational Notes

- Uploaded files and database data live under `data/`.
- The default Compose stack is production-like and uses `docker-compose.yml`.
- The development stack uses `docker-compose.dev.yml` and serves the app on port `8888`.
- RAG and Text2SQL use the local `llm-parser` container.
- The project disables and filters LLM reasoning/thinking output by default. See [LLM_REASONING_POLICY.md](LLM_REASONING_POLICY.md).
- QueryDSL parsing is experimental. The default search/RAG path still uses semantic query passthrough.

## Release Notes

For `0.1.0-alpha`, the main focus is:

- Full-stack RAG integration.
- Hybrid retrieval improvements.
- Contextual compression for evidence.
- Server-side sync API groundwork.
- Django admin operational monitoring.
- LLM output policy documentation.

## Roadmap

- Retrieval evaluation with a larger golden set.
- RAG quality evaluation and citation navigation improvements.
- More complete Text2SQL workflow.
- Optional QueryDSL parser rollout after evaluation.
- Backup, security, and monitoring documentation for production use.
