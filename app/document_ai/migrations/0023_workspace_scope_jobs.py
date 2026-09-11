import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_workspaces(apps, schema_editor):
    Membership = apps.get_model("workspaces", "WorkspaceMembership")
    models_to_fill = [
        apps.get_model("document_ai", "QueryUnderstandingLog"),
        apps.get_model("document_ai", "SearchJob"),
        apps.get_model("document_ai", "RAGJob"),
    ]
    owner_ids = set()
    for Model in models_to_fill:
        owner_ids.update(
            Model.objects.filter(workspace__isnull=True)
            .values_list("owner_id", flat=True)
            .distinct()
        )
    for owner_id in owner_ids:
        if owner_id is None:
            continue
        workspace_id = Membership.objects.filter(
            user_id=owner_id,
            status="active",
            workspace__kind="personal",
        ).values_list("workspace_id", flat=True).first()
        if workspace_id:
            for Model in models_to_fill:
                Model.objects.filter(owner_id=owner_id, workspace__isnull=True).update(
                    workspace_id=workspace_id
                )


class Migration(migrations.Migration):
    dependencies = [
        ("document_ai", "0022_resourcesnapshot"),
        ("workspaces", "0003_secure_invite_codes_and_creator_lifecycle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="queryunderstandinglog",
            name="workspace",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="query_understanding_logs", to="workspaces.workspace"),
        ),
        migrations.AddField(
            model_name="searchjob",
            name="workspace",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="search_jobs", to="workspaces.workspace"),
        ),
        migrations.AddField(
            model_name="ragjob",
            name="workspace",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="rag_jobs", to="workspaces.workspace"),
        ),
        migrations.RunPython(backfill_workspaces, migrations.RunPython.noop),
        migrations.AlterField(model_name="queryunderstandinglog", name="workspace", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="query_understanding_logs", to="workspaces.workspace")),
        migrations.AlterField(model_name="searchjob", name="workspace", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="search_jobs", to="workspaces.workspace")),
        migrations.AlterField(model_name="ragjob", name="workspace", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rag_jobs", to="workspaces.workspace")),
        migrations.AlterField(model_name="queryunderstandinglog", name="owner", field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="document_ai_query_understanding_logs", to=settings.AUTH_USER_MODEL)),
        migrations.AlterField(model_name="searchjob", name="owner", field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="document_ai_search_jobs", to=settings.AUTH_USER_MODEL)),
        migrations.AlterField(model_name="ragjob", name="owner", field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="document_ai_rag_jobs", to=settings.AUTH_USER_MODEL)),
        migrations.AddIndex(model_name="queryunderstandinglog", index=models.Index(fields=["workspace", "mode", "-created_at"], name="docai_qu_ws_mode_created_idx")),
        migrations.AddIndex(model_name="searchjob", index=models.Index(fields=["workspace", "status", "-created_at"], name="docai_se_ws_status_created_idx")),
        migrations.AddIndex(model_name="ragjob", index=models.Index(fields=["workspace", "status", "-created_at"], name="docai_ra_ws_status_created_idx")),
    ]
