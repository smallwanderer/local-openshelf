import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR.parent / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return [item.strip() for item in raw_value.split(",") if item.strip()]


_load_env_file(ENV_FILE)


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = _env_list(
    "DOTORI_DJANGO_ALLOWED_HOSTS",
    ["localhost", "127.0.0.1"],
)

# SESSION SETTINGS
SESSION_COOKIE_AGE = 60 * 60 * 24 * 3 # 3 days
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.postgres',
    'django.contrib.staticfiles',

    'files.apps.FilesConfig',
    'accounts.apps.AccountsConfig',
    'document_ai.apps.DocumentAiConfig',
    'workspaces.apps.WorkspacesConfig',
    'rest_framework',
    'drf_yasg',
    'django_celery_results',
]

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "accounts.api_responses.drf_exception_handler",
}

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/files/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# The production image builds the Vite SPA into app/static/spa. Operators can
# immediately roll the root entry point back to the retained Django templates
# without rebuilding the image.
SPA_ENABLED = _env_bool("DOTORI_SPA_ENABLED", True)
SPA_MANIFEST_PATH = Path(
    os.getenv(
        "DOTORI_SPA_MANIFEST_PATH",
        str(BASE_DIR / "static" / "spa" / ".vite" / "manifest.json"),
    )
)

# 개인/로컬 배포 기본값: 로그인 없이 로컬 관리자 프로필로 자동 진입한다.
# 외부 접속을 허용하는 배포에서는 1로 설정해 실제 로그인을 요구한다.
LOGIN_REQUIRED = _env_bool("LOGIN_REQUIRED", False)

MEDIA_URL = os.getenv("MEDIA_URL", "/media/")
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(BASE_DIR / "media")))

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'config.middleware.TraceIdMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.middleware.LocalProfileMiddleware',
    'workspaces.middleware.ActiveWorkspaceMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CSRF_TRUSTED_ORIGINS = _env_list(
    "DOTORI_DJANGO_CSRF_TRUSTED_ORIGINS",
    [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
)
CSRF_FAILURE_VIEW = "accounts.views.csrf_failure"

SECURE_SSL_REDIRECT = _env_bool("DOTORI_DJANGO_SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = _env_bool("DOTORI_DJANGO_SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = _env_bool("DOTORI_DJANGO_CSRF_COOKIE_SECURE", False)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = _env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)

if _env_bool("DOTORI_DJANGO_SECURE_PROXY_SSL_HEADER", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Only the process gunicorn boots as embedding-executor's model owner (see
# DOTORI_EMBEDDING_MODEL_PROCESS in docker-compose.yml) gets the tiny internal
# urlconf (livez/readyz/embed). Every other process -- app and the embedding
# queue consumer -- uses the normal user-facing urlconf, which no longer
# defines any internal embedding path at all.
ROOT_URLCONF = (
    'config.embedding_urls'
    if os.getenv("DOTORI_EXECUTOR_ROLE") in {"parser", "embedding"}
    or os.getenv("DOTORI_EMBEDDING_MODEL_PROCESS") == "1"
    else 'config.urls'
)

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / "templates"],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'files.context_processors.storage_usage',
                'accounts.context_processors.login_mode',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Logging
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR.parent / "data" / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# app/embedding-executor 컨테이너가 같은 LOG_DIR을 공유하므로, 서비스별로 로그 파일을
# 분리해 동시 쓰기 경합(같은 파일에 두 프로세스가 쓰는 문제)을 피한다.
SERVICE_NAME = os.getenv("SERVICE_NAME", "app")

OPERATION_LOG_LEVEL = os.getenv("OPERATION_LOG_LEVEL", "WARNING")

# Shared secret app/consumer <-> embedding-executor sent on internal /embed calls.
# Never sent to browsers/SPA. Generated by install.py; if unset, the internal
# view fails closed (401) rather than accepting unauthenticated requests.
EMBEDDING_INTERNAL_TOKEN = os.getenv("EMBEDDING_INTERNAL_TOKEN", "")
EXECUTOR_INTERNAL_TOKEN = os.getenv("EXECUTOR_INTERNAL_TOKEN", EMBEDDING_INTERNAL_TOKEN)
DOCUMENT_AI_LOG_LEVEL = os.getenv("DOCUMENT_AI_LOG_LEVEL", "INFO")
FILES_LOG_LEVEL = os.getenv("FILES_LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "trace_id": {
            "()": "config.tracing.TraceIdLogFilter",
        },
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s [%(levelname)s] %(name)s %(process)d [trace_id=%(trace_id)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["trace_id"],
            "formatter": "standard",
        },
        "operations_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": OPERATION_LOG_LEVEL,
            "filename": LOG_DIR / f"operations.{SERVICE_NAME}.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "standard",
            "filters": ["trace_id"],
            "encoding": "utf-8",
        },
        "document_ai_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": DOCUMENT_AI_LOG_LEVEL,
            "filename": LOG_DIR / f"document_ai.{SERVICE_NAME}.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "standard",
            "filters": ["trace_id"],
            "encoding": "utf-8",
        },
        "db_span_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "INFO",
            "filename": LOG_DIR / f"db_span.{SERVICE_NAME}.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "standard",
            "filters": ["trace_id"],
            "encoding": "utf-8",
        },
        "files_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": FILES_LOG_LEVEL,
            "filename": LOG_DIR / f"files.{SERVICE_NAME}.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "standard",
            "filters": ["trace_id"],
            "encoding": "utf-8",
        },
    },
    "root": {
        "handlers": ["console", "operations_file"],
        "level": OPERATION_LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console", "operations_file"],
            "level": OPERATION_LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "operations_file"],
            "level": "ERROR",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "operations_file"],
            "level": OPERATION_LOG_LEVEL,
            "propagate": False,
        },
        "document_ai": {
            "handlers": ["console", "operations_file", "document_ai_file"],
            "level": DOCUMENT_AI_LOG_LEVEL,
            "propagate": False,
        },
        "db_span": {
            "handlers": ["db_span_file"],
            "level": "INFO",
            "propagate": False,
        },
        "files": {
            "handlers": ["console", "operations_file", "files_file"],
            "level": FILES_LOG_LEVEL,
            "propagate": False,
        },
    },
}

# Celery
CELERY_TIMEZONE = 'Asia/Seoul'
CELERY_ENABLE_UTC = False

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_RESULT_EXTENDED = True  # args, kwargs, worker, retries 등 상세 정보 저장
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 30
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600, "queue_order_strategy": "round_robin"}
DOCUMENT_AI_RECOVERY_STALE_MINUTES = int(os.getenv("DOCUMENT_AI_RECOVERY_STALE_MINUTES", "30"))
DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE = int(os.getenv("DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE", "50"))
DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE = int(os.getenv("DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE", "200"))


# Document AI — Parser & Chunker
# Note: When an active embedding runtime (embedding_runtime.json) is present,
# tokenizer identity and max_tokens are resolved dynamically from its contract.
# The settings below serve as safe fallbacks when the runtime file is uninitialized.
DOCUMENT_PARSER_BACKEND = os.getenv("DOCUMENT_PARSER_BACKEND", "docling")
PARSER_TOKENIZER_ID = os.getenv("PARSER_TOKENIZER_ID", "BAAI/bge-m3")
PARSER_TOKENIZER_REVISION = os.getenv("PARSER_TOKENIZER_REVISION", "5617a9f")
EMBEDDING_MAX_TOKENS = int(os.getenv("EMBEDDING_MAX_TOKENS", "1280"))
EMBEDDING_TOKEN_HEADROOM = int(os.getenv("EMBEDDING_TOKEN_HEADROOM", "128"))
if EMBEDDING_MAX_TOKENS <= EMBEDDING_TOKEN_HEADROOM:
    raise ValueError(
        "EMBEDDING_MAX_TOKENS must be greater than EMBEDDING_TOKEN_HEADROOM."
    )
CHUNK_MAX_TOKENS = EMBEDDING_MAX_TOKENS - EMBEDDING_TOKEN_HEADROOM
_search_query_embedding_max_tokens_raw = os.getenv("SEARCH_QUERY_EMBEDDING_MAX_TOKENS")
if _search_query_embedding_max_tokens_raw:
    SEARCH_QUERY_EMBEDDING_MAX_TOKENS = int(_search_query_embedding_max_tokens_raw)

# Same-document chunk micro-batching: group a document's PENDING chunks into
# one model.encode() call instead of one call per chunk. Bounded by count AND
# token budget -- a bigger batch holds the shared embedding admission slot
# longer, which delays any query waiting behind it.
EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS = int(os.getenv("EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS", "4"))
EMBEDDING_DOCUMENT_BATCH_MAX_TOKENS = int(os.getenv("EMBEDDING_DOCUMENT_BATCH_MAX_TOKENS", "3200"))

# Document AI — Retriever (Hybrid Search)
EMBEDDING_DOC_POOLING_METHOD = os.getenv("EMBEDDING_DOC_POOLING_METHOD", "normalized_logsumexp")
EMBEDDING_HYBRID_DENSE_WEIGHT = float(os.getenv("EMBEDDING_HYBRID_DENSE_WEIGHT", "0.3"))
EMBEDDING_HYBRID_SPARSE_WEIGHT = float(os.getenv("EMBEDDING_HYBRID_SPARSE_WEIGHT", "0.7"))
EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER = int(os.getenv("EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER", "12"))
EMBEDDING_PER_NODE_CANDIDATE_CAP = int(os.getenv("EMBEDDING_PER_NODE_CANDIDATE_CAP", "4"))
EMBEDDING_QUERY_SPARSE_TOP_N = int(os.getenv("EMBEDDING_QUERY_SPARSE_TOP_N", "32"))
EMBEDDING_EVIDENCE_TOP_K = int(os.getenv("EMBEDDING_EVIDENCE_TOP_K", "3"))
EMBEDDING_DOC_POOL_TOP_K = int(os.getenv("EMBEDDING_DOC_POOL_TOP_K", "5"))
EMBEDDING_DOC_POOL_TAU = float(os.getenv("EMBEDDING_DOC_POOL_TAU", "5.0"))
EMBEDDING_DOC_LENGTH_PENALTY_ALPHA = float(os.getenv("EMBEDDING_DOC_LENGTH_PENALTY_ALPHA", "0.10"))
EMBEDDING_EVIDENCE_CONTEXT_WINDOW = int(os.getenv("EMBEDDING_EVIDENCE_CONTEXT_WINDOW", "1"))
CONTEXTUAL_COMPRESSION_ENABLED = os.getenv("CONTEXTUAL_COMPRESSION_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
CONTEXTUAL_COMPRESSION_WINDOW_SIZE = int(os.getenv("CONTEXTUAL_COMPRESSION_WINDOW_SIZE", "2"))
CONTEXTUAL_COMPRESSION_MAX_SEGMENTS_PER_CHUNK = int(os.getenv("CONTEXTUAL_COMPRESSION_MAX_SEGMENTS_PER_CHUNK", "16"))
CONTEXTUAL_COMPRESSION_TOP_SEGMENTS = int(os.getenv("CONTEXTUAL_COMPRESSION_TOP_SEGMENTS", "3"))
CONTEXTUAL_COMPRESSION_MAX_CHARS = int(os.getenv("CONTEXTUAL_COMPRESSION_MAX_CHARS", "700"))
CONTEXTUAL_COMPRESSION_MIN_SCORE = float(os.getenv("CONTEXTUAL_COMPRESSION_MIN_SCORE", "0.1"))
CONTEXTUAL_COMPRESSION_DENSE_WEIGHT = float(os.getenv("CONTEXTUAL_COMPRESSION_DENSE_WEIGHT", "0.4"))
CONTEXTUAL_COMPRESSION_SPARSE_WEIGHT = float(os.getenv("CONTEXTUAL_COMPRESSION_SPARSE_WEIGHT", "0.6"))
RAG_SEARCH_TOP_K = int(os.getenv("RAG_SEARCH_TOP_K", "3"))
_rag_retrieval_threshold_raw = os.getenv("RAG_RETRIEVAL_THRESHOLD", "0.35").strip()
RAG_RETRIEVAL_THRESHOLD = float(_rag_retrieval_threshold_raw) if _rag_retrieval_threshold_raw else None
RAG_MAX_TOKENS = int(os.getenv("RAG_MAX_TOKENS", "512"))
RAG_TEMPERATURE = float(os.getenv("RAG_TEMPERATURE", "0.2"))
RAG_TOP_P = float(os.getenv("RAG_TOP_P", "0.9"))


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": os.getenv("DB_ENGINE", "django.db.backends.postgresql"),
        "NAME": os.getenv("POSTGRES_DB", "filehub"),
        "USER": os.getenv("POSTGRES_USER", "filehub"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

# AUTH_USER_MODEL
AUTH_USER_MODEL = "accounts.User"

# for_development
EMAIL_BACKEND = os.getenv(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@example.com")
REQUIRE_EMAIL_VERIFICATION = _env_bool("REQUIRE_EMAIL_VERIFICATION", True)

# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = os.getenv("DJANGO_LANGUAGE_CODE", "ko-kr")

TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Seoul")

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
