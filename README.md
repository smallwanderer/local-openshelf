# Dotori Documents

Dotori is a self-hosted document workspace for document management, hybrid search, and retrieval-augmented generation (RAG) on a single server. Documents, search indexes, and local AI runtimes remain in an operator-managed Docker Compose environment.

<p align="center">
  <img src="https://github.com/user-attachments/assets/54c7a4a6-39cd-49f9-b5ad-99fe4b24a438" width="80%" alt="Dotori document workspace">
</p>

For Korean documentation, see [README.ko.md](README.ko.md). For the full documentation set (installation, operations, monitoring, API), start at [documents/](documents/WALKTHROUGH.md).

## Key Features

- Multiple accounts with secure separation of files and document features
- Folder and file management with authentication, trash, favorites, and recent files
- Analysis of PDF, HWP, DOCX, and other text-based document formats
- Natural-language document search
- Local RAG that answers questions from document contents without sending data to an external provider
- Guided installation and use of a local LLM selected for the server's hardware
- Three installation modes, from file management only to a complete local RAG stack
- Korean and English web interfaces, with support for external AI models
- Optional external, OpenAI-compatible embedding endpoints (OpenAI, vLLM, Ollama, or a custom server) in place of the local BGE-M3 model

> [!CAUTION]
> The operator must select the local LLM. AI models and runtimes are managed as server-wide settings, not per-user settings.
>
> When an external LLM such as ChatGPT or Claude is selected, document content may be sent to the external provider.

## Installation Modes

The included installation assistant, `start.bat`, lets operators set up the server without writing code. See the [installation guide](documents/installation-guide.md) for details.

The installer provides the following options:

1. Optional feature activation, including enabling or disabling natural-language search and RAG
2. Local LLM installation guidance
3. Server environment configuration guidance
4. External-access domain configuration

## Quick Start

### Requirements

- Docker Engine or Docker Desktop
- Python 3.x
- An NVIDIA driver and NVIDIA Container Toolkit when using the vLLM GPU runtime
- A Hugging Face token when the selected model requires authentication

Docker Desktop with the WSL2 backend is recommended on Windows.

## System Architecture

```text
      Nginx
        |
     Web App -------------- Database
        |                       |
        +-- synchronous search -+
        +-- HTTP RAG stream -------- selected local runtime
        |                               |-- llama.cpp
        |                               `-- vLLM
        `-- Queue Server
              |-- parser-executor              [parse]
              `-- embedding-executor-consumer [embed]
                         `-- embedding-executor [HTTP model/backend]
```

Only long-running parsing and embedding work is queued. Interactive search returns directly, and RAG answers stream over HTTP.

### Planned Interface Architecture

Dotori is moving gradually from Django template-rendered screens to an API-centered server with a separate `web/` SPA. Django continues to own authentication, authorization, documents, search, RAG, and operator functions. The `web/` frontend is a small, lightweight React SPA that requires no separate Node.js server in production. The GUI and the independent `dotori-cli` reuse the same APIs, which may later support an optional thin MCP adapter.

This is a design direction, not a completed feature. See the [API-centered server and lightweight SPA transition plan](documents/interface-architecture-plan.md) for the intended boundaries and migration sequence.

## Data and Configuration

- `.env.example` contains the primary operational settings. Copy it to `.env` and adjust it as needed.
- `.env.example` also contains controls for advanced features.
- All files saved through Dotori are stored on the local server filesystem. Dotori does not currently provide a separate backup feature.
- `data/config/llm_runtime.json` is generated during local RAG setup and serves as the server-wide source of truth for the selected runtime.
- The embedding backend can be switched from the local BGE-M3 model to an external OpenAI-compatible endpoint through `.env` and `data/config/runtime_scopes/production/embedding_runtime.json`; see the [embedding guide](documents/embedding-guide.md) for supported vector dimensions and setup steps.

## Development

The repository's primary application boundaries are `files`, `accounts`, and `document_ai`. Heavy AI work is separated into processing, embedding, query-understanding, search, RAG, installation, and runtime modules.

The repository includes tests for selected behavior:

```bash
docker compose --profile test run --rm test python manage.py check
docker compose --profile test run --rm test python -m pytest
```

## Project Status

Dotori is under active development. The current source provides the features described above and includes experimental functionality. Please report bugs and feature requests through an issue or by email.

## License

See [LICENSE](LICENSE).
