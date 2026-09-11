import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_workspaces(apps, schema_editor):
    Membership = apps.get_model("workspaces", "WorkspaceMembership")
    LLMEndpoint = apps.get_model("document_ai", "LLMEndpoint")
    UserLLMPreference = apps.get_model("document_ai", "UserLLMPreference")

    owner_ids = set(
        LLMEndpoint.objects.filter(workspace__isnull=True)
        .values_list("owner_id", flat=True)
        .distinct()
    )
    owner_ids.update(
        UserLLMPreference.objects.filter(workspace__isnull=True)
        .values_list("user_id", flat=True)
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
        if not workspace_id:
            continue
        LLMEndpoint.objects.filter(owner_id=owner_id, workspace__isnull=True).update(
            workspace_id=workspace_id
        )
        UserLLMPreference.objects.filter(user_id=owner_id, workspace__isnull=True).update(
            workspace_id=workspace_id
        )


class Migration(migrations.Migration):
    dependencies = [
        ("document_ai", "0023_workspace_scope_jobs"),
        ("workspaces", "0004_workspaceinvitation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="llmendpoint",
            name="workspace",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_endpoints",
                to="workspaces.workspace",
            ),
        ),
        migrations.AddField(
            model_name="userllmpreference",
            name="workspace",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_preference",
                to="workspaces.workspace",
            ),
        ),
        migrations.RunPython(backfill_workspaces, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="llmendpoint",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_endpoints",
                to="workspaces.workspace",
            ),
        ),
        migrations.AlterField(
            model_name="userllmpreference",
            name="workspace",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_preference",
                to="workspaces.workspace",
            ),
        ),
        migrations.AlterField(
            model_name="llmendpoint",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="document_ai_llm_endpoints",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="userllmpreference",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="legacy_llm_preference",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="llmendpoint",
            name="uniq_llm_endpoint_name_per_owner",
        ),
        migrations.AddConstraint(
            model_name="llmendpoint",
            constraint=models.UniqueConstraint(
                fields=("workspace", "name"),
                name="uniq_llm_endpoint_name_per_workspace",
            ),
        ),
        migrations.AddIndex(
            model_name="llmendpoint",
            index=models.Index(fields=["workspace", "is_active"], name="llmendpoint_ws_active_idx"),
        ),
    ]
