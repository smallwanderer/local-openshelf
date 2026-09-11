import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0005_rename_personal_workspaces_possessive"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkspaceQualityProfileRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uid", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ("version", models.PositiveIntegerField()),
                ("revision", models.PositiveIntegerField(default=1)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("active", "Active"), ("archived", "Archived")], db_index=True, max_length=16)),
                ("change_axis", models.CharField(blank=True, choices=[("retrieval", "Retrieval"), ("generation", "Generation"), ("prompt_policy", "Prompt policy")], max_length=16, null=True)),
                ("schema_version", models.PositiveIntegerField(default=1)),
                ("retrieval_config", models.JSONField(blank=True, default=dict)),
                ("generation_config", models.JSONField(blank=True, default=dict)),
                ("prompt_policy", models.JSONField(blank=True, default=dict)),
                ("validation_state", models.CharField(default="not_run", max_length=16)),
                ("validation_warnings", models.JSONField(blank=True, default=list)),
                ("applied_evaluation_run_uid", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.CharField(blank=True, max_length=500)),
                ("based_on", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="derived_revisions", to="workspaces.workspacequalityprofilerevision")),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_quality_profile_revisions", to=settings.AUTH_USER_MODEL)),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="quality_profile_revisions", to="workspaces.workspace")),
            ],
            options={"ordering": ["-version", "-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="workspacequalityprofilerevision",
            constraint=models.UniqueConstraint(fields=("workspace", "version"), name="uniq_quality_profile_workspace_version"),
        ),
        migrations.AddConstraint(
            model_name="workspacequalityprofilerevision",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "active")), fields=("workspace",), name="uniq_active_quality_profile_workspace"),
        ),
        migrations.AddConstraint(
            model_name="workspacequalityprofilerevision",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "draft")), fields=("workspace",), name="uniq_draft_quality_profile_workspace"),
        ),
        migrations.AddConstraint(
            model_name="workspacequalityprofilerevision",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("status", "draft"), _negated=True), ("change_axis__isnull", False), _connector="OR"), name="draft_quality_profile_requires_axis"),
        ),
    ]
