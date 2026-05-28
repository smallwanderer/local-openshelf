import secrets

from django.db import models
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.auth.base_user import BaseUserManager

class UserManager(BaseUserManager):
    def create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        if not password:
            raise ValueError("Users must have a password")
        email = self.normalize_email(email)
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_active", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("email_verified", False)
        extra_fields.setdefault("email_verification_sent_at", None)

        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Superusers must have an email address")
        if not password:
            raise ValueError("Superusers must have a password")
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("email_verified", True)
        extra_fields.setdefault("email_verification_sent_at", None)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    email_verified = models.BooleanField(default=False)
    email_verification_sent_at = models.DateTimeField(blank=True, null=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email


def _generate_token_key():
    return secrets.token_hex(32)


class APIToken(models.Model):
    """Bearer token for CLI / programmatic access."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    key = models.CharField(
        max_length=64, unique=True, db_index=True, default=_generate_token_key
    )
    name = models.CharField(max_length=128, help_text="Token purpose description")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["key"])]

    def __str__(self):
        return f"{self.name} ({self.user.email})"


class SyncQuota(models.Model):
    """Separate storage quota for sync uploads."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sync_quota",
    )
    total_size = models.BigIntegerField(default=10 * 1024 * 1024 * 1024)  # 10 GB
    used_size = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def remaining_size(self):
        return max(self.total_size - self.used_size, 0)

    def __str__(self):
        used_mb = round(self.used_size / 1024 / 1024, 2)
        total_gb = round(self.total_size / 1024 / 1024 / 1024, 2)
        return f"SyncQuota({self.user.email}) {used_mb}MB / {total_gb}GB"