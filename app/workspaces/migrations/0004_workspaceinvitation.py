import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0003_secure_invite_codes_and_creator_lifecycle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkspaceInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("declined", "Declined"), ("revoked", "Revoked")], db_index=True, default="pending", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("responded_at", models.DateTimeField(blank=True, null=True)),
                ("invited_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="issued_workspace_invitations", to=settings.AUTH_USER_MODEL)),
                ("invitee", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="workspace_invitations", to=settings.AUTH_USER_MODEL)),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="invitations", to="workspaces.workspace")),
            ],
        ),
        migrations.AddIndex(model_name="workspaceinvitation", index=models.Index(fields=["invitee", "status", "-created_at"], name="workspace_i_invitee_status_idx")),
        migrations.AddIndex(model_name="workspaceinvitation", index=models.Index(fields=["workspace", "status", "-created_at"], name="workspace_i_ws_status_idx")),
    ]
