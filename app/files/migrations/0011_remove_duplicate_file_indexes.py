from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("files", "0010_userstorage_workspace_required")]

    operations = [
        migrations.RemoveIndex(model_name="node", name="node_workspace_path_idx"),
        migrations.RemoveIndex(model_name="fileblob", name="files_fileb_sha256_2fe16f_idx"),
        migrations.RemoveIndex(model_name="fileblob", name="files_fileb_status_b08e23_idx"),
    ]
