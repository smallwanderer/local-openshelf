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
    "DJANGO_ALLOWED_HOSTS",
    ["*"] if DEBUG else ["localhost", "127.0.0.1"],
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
    'django.contrib.staticfiles',

    'files.apps.FilesConfig',
    'accounts.apps.AccountsConfig',
    'document_ai.apps.DocumentAiConfig',
    'rest_framework',
    'drf_yasg',
    'django_celery_results',
]

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/files/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

MEDIA_URL = os.getenv("MEDIA_URL", "/media/")
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(BASE_DIR / "media")))

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CSRF_TRUSTED_ORIGINS = _env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    ["http://localhost"],
)

ROOT_URLCONF = 'config.urls'

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
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Celery
CELERY_TIMEZONE = 'Asia/Seoul'
CELERY_ENABLE_UTC = False

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_RESULT_EXTENDED = True  # args, kwargs, worker, retries 등 상세 정보 저장
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 30
DOCUMENT_AI_RECOVERY_INTERVAL_SECONDS = int(os.getenv("DOCUMENT_AI_RECOVERY_INTERVAL_SECONDS", "300"))
DOCUMENT_AI_RECOVERY_STALE_MINUTES = int(os.getenv("DOCUMENT_AI_RECOVERY_STALE_MINUTES", "30"))
DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE = int(os.getenv("DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE", "50"))
DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE = int(os.getenv("DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE", "200"))
CELERY_BEAT_SCHEDULE = {
    "recover-document-pipeline-backlog": {
        "task": "document_ai.tasks.recover_document_pipeline_backlog",
        "schedule": DOCUMENT_AI_RECOVERY_INTERVAL_SECONDS,
    },
}


# Document AI — Parser & Chunker
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "bgem3_hybrid")
CHUNK_MAX_TOKENS = int(os.getenv("CHUNK_MAX_TOKENS", "1024"))
EMBEDDING_TOKEN_HEADROOM = int(os.getenv("EMBEDDING_TOKEN_HEADROOM", "256"))
# EMBEDDING_MAX_TOKENS: 미설정 시 config.py 가 CHUNK_MAX_TOKENS + HEADROOM 으로 계산
_embedding_max_tokens_raw = os.getenv("EMBEDDING_MAX_TOKENS")
if _embedding_max_tokens_raw:
    EMBEDDING_MAX_TOKENS = int(_embedding_max_tokens_raw)

# Document AI — Retriever (Hybrid Search)
EMBEDDING_HYBRID_DENSE_WEIGHT = float(os.getenv("EMBEDDING_HYBRID_DENSE_WEIGHT", "0.5"))
EMBEDDING_HYBRID_SPARSE_WEIGHT = float(os.getenv("EMBEDDING_HYBRID_SPARSE_WEIGHT", "0.5"))
EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER = int(os.getenv("EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER", "12"))
EMBEDDING_PER_NODE_CANDIDATE_CAP = int(os.getenv("EMBEDDING_PER_NODE_CANDIDATE_CAP", "4"))
EMBEDDING_QUERY_SPARSE_TOP_N = int(os.getenv("EMBEDDING_QUERY_SPARSE_TOP_N", "32"))
EMBEDDING_EVIDENCE_TOP_K = int(os.getenv("EMBEDDING_EVIDENCE_TOP_K", "3"))
EMBEDDING_DOC_POOL_TOP_K = int(os.getenv("EMBEDDING_DOC_POOL_TOP_K", "5"))
EMBEDDING_DOC_POOL_TAU = float(os.getenv("EMBEDDING_DOC_POOL_TAU", "5.0"))
EMBEDDING_DOC_LENGTH_PENALTY_ALPHA = float(os.getenv("EMBEDDING_DOC_LENGTH_PENALTY_ALPHA", "0.10"))
EMBEDDING_EVIDENCE_CONTEXT_WINDOW = int(os.getenv("EMBEDDING_EVIDENCE_CONTEXT_WINDOW", "1"))


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

STATIC_URL = 'static/'
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
