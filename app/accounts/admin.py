from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import APIToken, SyncQuota, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email", "is_active", "is_staff", "email_verified", "created_at")
    list_filter = ("is_active", "is_staff", "email_verified")
    search_fields = ("email",)
    ordering = ("-created_at",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "email_verified")}),
        ("Dates", {"fields": ("created_at", "email_verification_sent_at")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),
    )
    readonly_fields = ("created_at",)


@admin.register(APIToken)
class APITokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "key_preview", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("name", "user__email")
    readonly_fields = ("key", "created_at", "last_used_at")

    def key_preview(self, obj):
        return f"{obj.key[:8]}...{obj.key[-4:]}"
    key_preview.short_description = "Key"


@admin.register(SyncQuota)
class SyncQuotaAdmin(admin.ModelAdmin):
    list_display = ("user", "used_mb", "total_gb", "remaining_mb")
    search_fields = ("user__email",)

    def used_mb(self, obj):
        return round(obj.used_size / 1024 / 1024, 2)
    used_mb.short_description = "Used (MB)"

    def total_gb(self, obj):
        return round(obj.total_size / 1024 / 1024 / 1024, 2)
    total_gb.short_description = "Total (GB)"

    def remaining_mb(self, obj):
        return round(obj.remaining_size / 1024 / 1024, 2)
    remaining_mb.short_description = "Remaining (MB)"
