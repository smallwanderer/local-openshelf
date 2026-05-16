FROM python:3.12-slim

ENV HF_HOME=/app/.cache/huggingface \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG INSTALL_AI_DEPS=1
ARG INSTALL_DEV_DEPS=0

RUN if [ "$INSTALL_AI_DEPS" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            tesseract-ocr \
            tesseract-ocr-kor \
            libglib2.0-0 \
            libgl1 \
            libgomp1 \
            libsm6 \
            libxext6 \
            libxrender1 \
            poppler-utils \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.web.txt requirements.ai.txt requirements.dev.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements.web.txt \
    && if [ "$INSTALL_AI_DEPS" = "1" ]; then \
        pip install --no-cache-dir -r requirements.ai.txt; \
    fi \
    && if [ "$INSTALL_DEV_DEPS" = "1" ]; then \
        pip install --no-cache-dir -r requirements.dev.txt; \
    fi

RUN if [ "$INSTALL_AI_DEPS" = "1" ]; then \
        python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"; \
    fi

COPY ./ /app/

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
