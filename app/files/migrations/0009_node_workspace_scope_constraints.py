import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_workspace_storage(apps, schema_editor):
    FileBlob = apps.get_model("files", "FileBlob")
    UserStorage = apps.get_model("files", "UserStorage")
    Workspace = apps.get_model("workspaces", "Workspace")
    WorkspaceMembership = apps.get_model("workspaces", "WorkspaceMembership")

    for storage in UserStorage.objects.all().iterator():
        membership = WorkspaceMembership.objects.filter(
            user_id=storage.user_id,
            workspace__kind="personal",
        ).first()
        if membership:
            storage.workspace_id = membership.workspace_id
            storage.used_size = 0
            storage.save(update_fields=["workspace", "used_size"])
        else:
            storage.delete()

    for workspace in Workspace.objects.all().iterator():
        UserStorage.objects.get_or_create(
            workspace_id=workspace.id,
            defaults={"user_id": workspace.created_by_id},
        )

    totals = {}
    for blob in FileBlob.objects.select_related("node").iterator():
        workspace_id = blob.node.workspace_id
        totals[workspace_id] = totals.get(workspace_id, 0) + (blob.size or 0)
    for workspace_id, used_size in totals.items():
        UserStorage.objects.filter(workspace_id=workspace_id).update(used_size=used_size)


class Migration(migrations.Migration):

    dependencies = [
        ("files", "0008_node_workspace_required"),
        ("workspaces", "0003_secure_invite_codes_and_creator_lifecycle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="node",
            name="uniq_node_path_per_owner",
        ),
        migrations.RemoveIndex(model_name="node", name="files_node_owner_i_10f64d_idx"),
        migrations.RemoveIndex(model_name="node", name="files_node_owner_i_4a78be_idx"),
        migrations.RemoveIndex(model_name="node", name="files_node_owner_i_2dc3fe_idx"),
        migrations.RemoveIndex(model_name="node", name="files_node_owner_i_df47d0_idx"),
        migrations.RemoveIndex(model_name="node", name="files_node_owner_i_b7b036_idx"),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["workspace", "parent"], name="node_workspace_parent_idx"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["workspace", "trashed"], name="node_workspace_trashed_idx"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["workspace", "node_type"], name="node_workspace_type_idx"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["workspace", "-created_at"], name="node_workspace_created_idx"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["workspace", "path"], name="node_workspace_path_idx"),
        ),
        migrations.AddConstraint(
            model_name="node",
            constraint=models.UniqueConstraint(
                fields=("workspace", "path"),
                name="uniq_node_path_per_workspace",
            ),
        ),
        migrations.AlterField(
            model_name="node",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="owned_nodes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="userstorage",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="legacy_storage_records",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="userstorage",
            name="workspace",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="storage",
                to="workspaces.workspace",
            ),
        ),
        migrations.RunPython(backfill_workspace_storage, migrations.RunPython.noop),
    ]
