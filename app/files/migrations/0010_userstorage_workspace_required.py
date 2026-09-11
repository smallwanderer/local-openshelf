import django.db.models.deletion
from django.db import migrations, models


# Split out from 0009: in the same transaction as that migration's backfill
# RunPython, this ALTER TABLE fails against a populated database with
# "cannot ALTER TABLE ... because it has pending trigger events" (Postgres
# won't run DDL on files_userstorage while the backfill's UPDATE/DELETE still
# has deferred FK-constraint triggers pending in that transaction).
class Migration(migrations.Migration):
    dependencies = [
        ("files", "0009_node_workspace_scope_constraints"),
    ]

    operations = [
        migrations.AlterField(
            model_name="userstorage",
            name="workspace",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="storage",
                to="workspaces.workspace",
            ),
        ),
    ]
