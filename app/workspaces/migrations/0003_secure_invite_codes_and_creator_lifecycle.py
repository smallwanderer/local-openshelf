import hashlib

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def hash_existing_codes(apps, schema_editor):
    InviteCode = apps.get_model("workspaces", "WorkspaceInviteCode")
    for invite in InviteCode.objects.all().iterator():
        token = invite.code or ""
        invite.code_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        invite.code_prefix = token[:8]
        invite.save(update_fields=["code_digest", "code_prefix"])


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0002_backfill_personal_workspaces"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workspace",
            name="created_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_workspaces",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="workspaceinvitecode",
            name="code_digest",
            field=models.CharField(db_index=True, max_length=64, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="workspaceinvitecode",
            name="code_prefix",
            field=models.CharField(db_index=True, max_length=12, null=True),
        ),
        migrations.RunPython(hash_existing_codes, migrations.RunPython.noop),
        migrations.RemoveField(model_name="workspaceinvitecode", name="code"),
        migrations.AlterField(
            model_name="workspaceinvitecode",
            name="code_digest",
            field=models.CharField(db_index=True, max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="workspaceinvitecode",
            name="code_prefix",
            field=models.CharField(db_index=True, max_length=12),
        ),
    ]
