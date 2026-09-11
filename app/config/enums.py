from django.db import models


class NodeType(models.TextChoices):
    FILE = "file", "File"
    FOLDER = "directory", "Directory"


class FileStatus(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class FileLanguage(models.TextChoices):
    KOREAN = "ko", "Korean"
    ENGLISH = "en", "English"
    CHINESE = "zh", "Chinese"
    JAPANESE = "ja", "Japanese"
    OTHER = "other", "Other"


class FileOperation(models.TextChoices):
    UPLOAD = "upload", "Upload"
    RENAME = "rename", "Rename"
    MOVE = "move", "Move"
    DELETE = "delete", "Delete"
    RESTORE = "restore", "Restore"
    PERMANENT_DELETE = "permanent_delete", "Permanent delete"
    EMPTY_TRASH = "empty_trash", "Empty trash"


class AIStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELED = "canceled", "Canceled"


class RAGStage(models.TextChoices):
    QUEUED = "queued", "Queued"
    SEARCHING = "searching", "Searching"
    GENERATING = "generating", "Generating"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELED = "canceled", "Canceled"


class QueryIntent(models.TextChoices):
    DOCUMENT_QUESTION = "document_question", "Document question"
    INFORMATION = "information", "Information"
    CASUAL_CHAT = "casual_chat", "Casual chat"
    APP_USAGE = "app_usage", "App usage"
    AMBIGUOUS = "ambiguous", "Ambiguous"


class QueryAnswerMode(models.TextChoices):
    RAG = "rag", "RAG"
    CASUAL = "casual", "Casual"
    APP_HELP = "app_help", "App help"
    GENERAL = "general", "General"
    AMBIGUOUS = "ambiguous", "Ambiguous"
