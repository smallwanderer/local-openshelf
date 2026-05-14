from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0003_documentchunk_updated_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentparseresult",
            name="recovery_attempts",
            field=models.PositiveIntegerField(
                default=0,
                help_text="복구 태스크가 재큐잉한 누적 횟수. MAX 초과 시 복구 후보에서 제외됩니다.",
            ),
        ),
        migrations.AddField(
            model_name="documentparseresult",
            name="last_recovered_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="마지막으로 복구 태스크가 재큐잉한 시각.",
            ),
        ),
        migrations.AddField(
            model_name="documentchunk",
            name="recovery_attempts",
            field=models.PositiveIntegerField(
                default=0,
                help_text="임베딩 복구 태스크가 재큐잉한 누적 횟수. MAX 초과 시 복구 후보에서 제외됩니다.",
            ),
        ),
        migrations.AddField(
            model_name="documentchunk",
            name="last_recovered_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="마지막으로 임베딩 복구 태스크가 재큐잉한 시각.",
            ),
        ),
    ]
