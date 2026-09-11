# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS backend

ENV HF_HOME=/app/.cache/huggingface \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG INSTALL_AI_DEPS=1
ARG INSTALL_PARSER_DOCLING=1
ARG INSTALL_TEST_DEPS=0

# tesseract/poppler는 docling의 OCR/PDF 파이프라인 전용이라 임베딩(AI_DEPS)이
# 아니라 파서 backend 선택(PARSER_DOCLING)을 따른다 -- docling 대신 다른 파서를
# 쓰는 이미지는 이 시스템 패키지를 설치할 필요가 없다.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    if [ "$INSTALL_PARSER_DOCLING" = "1" ]; then \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    poppler-utils; \
    fi

COPY app/requirements.web.txt app/requirements.ai.txt app/requirements.torch.txt app/requirements.parser-docling.txt app/requirements.test.txt app/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install -r requirements.web.txt \
    && if [ "$INSTALL_AI_DEPS" = "1" ]; then \
    pip install -r requirements.ai.txt; \
    fi \
    && if [ "$INSTALL_PARSER_DOCLING" = "1" ]; then \
    pip install -r requirements.parser-docling.txt; \
    fi \
    && if [ "$INSTALL_TEST_DEPS" = "1" ]; then \
    pip install -r requirements.test.txt; \
    fi

RUN if [ "$INSTALL_PARSER_DOCLING" = "1" ]; then \
    python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"; \
    fi

COPY app/ /app/

EXPOSE 8000

CMD ["gunicorn", "config.asgi:application", "--worker-class", "uvicorn_worker.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "2"]


FROM node:22-alpine AS web-builder

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked npm ci

COPY web/ ./
RUN npm run build


FROM backend AS web

COPY --from=web-builder /web/dist/assets/ /app/static/spa/assets/
COPY --from=web-builder /web/dist/.vite/ /app/static/spa/.vite/
COPY --from=web-builder /web/dist/icons/ /app/static/spa/icons/
COPY --from=web-builder /web/dist/images/ /app/static/spa/images/
