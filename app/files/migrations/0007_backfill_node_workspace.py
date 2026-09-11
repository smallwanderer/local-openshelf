from django.db import migrations


def backfill_node_workspace(apps, schema_editor):
    Node = apps.get_model("files", "Node")
    WorkspaceMembership = apps.get_model("workspaces", "WorkspaceMembership")

    owner_ids = Node.objects.filter(workspace__isnull=True).values_list("owner_id", flat=True).distinct()
    for owner_id in owner_ids:
        membership = WorkspaceMembership.objects.filter(
            user_id=owner_id, workspace__kind="personal"
        ).first()
        if membership is None:
            continue
        Node.objects.filter(owner_id=owner_id, workspace__isnull=True).update(
            workspace_id=membership.workspace_id
        )


class Migration(migrations.Migration):

    dependencies = [
        ("files", "0006_node_workspace"),
    ]

    operations = [
        migrations.RunPython(backfill_node_workspace, migrations.RunPython.noop),
    ]
