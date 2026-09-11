import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("files", "0007_backfill_node_workspace"),
    ]

    operations = [
        migrations.AlterField(
            model_name="node",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="nodes",
                to="workspaces.workspace",
            ),
        ),
    ]
