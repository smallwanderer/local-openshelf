from django.db import migrations


def backfill_personal_workspaces(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Workspace = apps.get_model("workspaces", "Workspace")
    WorkspaceMembership = apps.get_model("workspaces", "WorkspaceMembership")

    for user in User.objects.all().iterator():
        if WorkspaceMembership.objects.filter(
            user=user, workspace__kind="personal"
        ).exists():
            continue
        display_label = user.display_name or user.email
        workspace = Workspace.objects.create(
            name=f"{display_label}'s Workspace",
            kind="personal",
            created_by=user,
        )
        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=user,
            role="admin",
            status="active",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_personal_workspaces, migrations.RunPython.noop),
    ]
