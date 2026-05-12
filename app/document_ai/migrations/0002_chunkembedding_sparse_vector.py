from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="chunkembedding",
            name="sparse_vector",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
