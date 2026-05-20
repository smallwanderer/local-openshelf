from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0006_rename_document_ai_owner_i_34b3e2_idx_document_ai_owner_i_f2d395_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="searchjob",
            name="tuning_params",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
