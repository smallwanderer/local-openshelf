# Generated manually for the RAG conversation persistence boundary.

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0027_ragjob_docai_rag_ws_own_st_cmp_idx_and_more"),
        ("workspaces", "0008_merge_quality_profile_axes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RAGConversation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uid", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ("title", models.CharField(blank=True, max_length=160)),
                ("default_node_ids", models.JSONField(blank=True, default=list)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rag_conversations", to=settings.AUTH_USER_MODEL)),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rag_conversations", to="workspaces.workspace")),
            ],
            options={"ordering": ["-updated_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="ragconversation",
            index=models.Index(fields=["workspace", "-updated_at", "-id"], name="docai_conv_ws_updated_idx"),
        ),
        migrations.AddField(
            model_name="ragjob",
            name="conversation",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="rag_jobs", to="document_ai.ragconversation"),
        ),
        migrations.CreateModel(
            name="RAGMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uid", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ("sequence", models.PositiveIntegerField()),
                ("role", models.CharField(choices=[("user", "User"), ("assistant", "Assistant")], max_length=16)),
                ("content", models.TextField(blank=True)),
                ("client_request_id", models.UUIDField(blank=True, null=True)),
                ("node_ids", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="document_ai.ragconversation")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rag_messages", to=settings.AUTH_USER_MODEL)),
                ("rag_job", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="conversation_message", to="document_ai.ragjob")),
                ("reply_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="replies", to="document_ai.ragmessage")),
            ],
            options={"ordering": ["sequence", "id"]},
        ),
        migrations.AddConstraint(
            model_name="ragmessage",
            constraint=models.UniqueConstraint(fields=("conversation", "sequence"), name="docai_msg_conversation_sequence_uniq"),
        ),
        migrations.AddConstraint(
            model_name="ragmessage",
            constraint=models.UniqueConstraint(condition=models.Q(("client_request_id__isnull", False), ("role", "user")), fields=("conversation", "client_request_id"), name="docai_user_message_request_uniq"),
        ),
    ]
