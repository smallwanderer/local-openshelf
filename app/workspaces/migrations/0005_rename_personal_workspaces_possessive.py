from django.db import migrations


def rename_personal_workspaces(apps, schema_editor):
    """Personal workspace names created before this migration used
    "{name} Workspace"; new ones use the possessive "{name}'s Workspace".
    Re-derive and update existing rows so old and new accounts match.
    """
    Workspace = apps.get_model("workspaces", "Workspace")
    for workspace in Workspace.objects.filter(kind="personal").select_related("created_by").iterator():
        user = workspace.created_by
        if user is None:
            continue
        display_label = user.display_name or user.email
        workspace.name = f"{display_label}'s Workspace"
        workspace.save(update_fields=["name"])


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0004_workspaceinvitation"),
    ]

    operations = [
        migrations.RunPython(rename_personal_workspaces, migrations.RunPython.noop),
    ]
