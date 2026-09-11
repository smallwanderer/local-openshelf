# Generated manually for the executor dispatch boundary.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0028_rag_conversations"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExecutorJobReceipt",
            fields=[
                ("job_id", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("work_kind", models.CharField(max_length=64)),
                ("target_key", models.CharField(max_length=160)),
                ("payload_fingerprint", models.CharField(max_length=64)),
                ("source_snapshot", models.JSONField(blank=True, default=dict)),
                ("runtime_snapshot", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("running", "Running"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="running", max_length=16)),
                ("attempt", models.PositiveIntegerField(default=1)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error", models.JSONField(blank=True, default=dict)),
                ("retryable", models.BooleanField(default=False)),
                ("followups", models.JSONField(blank=True, default=list)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddIndex(
            model_name="executorjobreceipt",
            index=models.Index(fields=["work_kind", "target_key", "status"], name="docai_exec_target_status_idx"),
        ),
        migrations.AddIndex(
            model_name="executorjobreceipt",
            index=models.Index(fields=["status", "updated_at"], name="docai_exec_status_updated_idx"),
        ),
        migrations.AddConstraint(
            model_name="executorjobreceipt",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "running")), fields=("work_kind", "target_key"), name="docai_exec_one_active_target"),
        ),
    ]
