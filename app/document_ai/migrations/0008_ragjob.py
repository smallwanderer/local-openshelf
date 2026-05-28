from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("document_ai", "0007_searchjob_tuning_params"),
    ]

    operations = [
        migrations.CreateModel(
            name="RAGJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question", models.TextField()),
                ("top_k", models.PositiveIntegerField(default=5)),
                ("language", models.CharField(default="ko", max_length=8)),
                ("node_ids", models.JSONField(blank=True, default=list)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed")], db_index=True, default="pending", max_length=32)),
                ("task_id", models.CharField(blank=True, max_length=255)),
                ("answer", models.TextField(blank=True)),
                ("citations", models.JSONField(blank=True, default=list)),
                ("error_message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="document_ai_rag_jobs", to=settings.AUTH_USER_MODEL)),
                ("search_job", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rag_jobs", to="document_ai.searchjob")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="ragjob",
            index=models.Index(fields=["owner", "status", "-created_at"], name="document_ai_owner_i_92a1b8_idx"),
        ),
    ]
