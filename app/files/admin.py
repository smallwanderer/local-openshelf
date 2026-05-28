from django.contrib import admin

from files.models import FileBlob, Node, UserStorage


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner_email", "node_type", "ext", "path", "trashed", "ai_status_summary", "created_at", "updated_at")
    list_filter = ("node_type", "ext", "trashed", "starred", "created_at", "updated_at")
    search_fields = ("name", "path", "owner__email", "description")
    readonly_fields = ("uid", "created_at", "updated_at", "deleted_at", "ai_status_summary")
    raw_id_fields = ("owner", "parent")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("owner", "parent")

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.owner.email

    @admin.display(description="AI status")
    def ai_status_summary(self, obj):
        status = obj.get_ai_status()
        if not status:
            return "-"
        return f"parse={status['parse_status']}, embedding={status['embedding_status']} ({status['completed_chunks']}/{status['chunk_count']})"


@admin.register(FileBlob)
class FileBlobAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "node_name", "owner_email", "status", "language", "mime_type", "size_mb", "sha256", "created_at")
    list_filter = ("status", "language", "mime_type", "created_at")
    search_fields = ("original_name", "node__name", "node__owner__email", "sha256")
    readonly_fields = ("uuid", "file", "size", "sha256", "created_at")
    raw_id_fields = ("node",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("node", "node__owner")

    @admin.display(description="File")
    def node_name(self, obj):
        return obj.node.name

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.node.owner.email


@admin.register(UserStorage)
class UserStorageAdmin(admin.ModelAdmin):
    list_display = ("id", "user_email", "used_size_mb", "total_size_gb", "usage_percent", "updated_at")
    search_fields = ("user__email",)
    readonly_fields = ("used_size", "used_size_mb", "total_size_gb", "usage_percent", "updated_at")
    raw_id_fields = ("user",)
    ordering = ("-updated_at",)

    @admin.display(description="User")
    def user_email(self, obj):
        return obj.user.email
