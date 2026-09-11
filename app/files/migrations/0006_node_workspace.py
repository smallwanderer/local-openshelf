import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("files", "0005_fileoperationlog"),
        ("workspaces", "0002_backfill_personal_workspaces"),
    ]

    operations = [
        migrations.AlterField(
            model_name="node",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="owned_nodes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="node",
            name="workspace",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="nodes",
                to="workspaces.workspace",
            ),
        ),
    ]
