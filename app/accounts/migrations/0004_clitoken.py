import uuid

import accounts.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_user_display_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="CLIToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("key_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("prefix", models.CharField(max_length=20)),
                ("name", models.CharField(help_text="Token purpose description", max_length=128)),
                ("scopes", models.JSONField(default=accounts.models.default_cli_token_scopes)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cli_tokens", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["key_hash"], name="accounts_cl_key_has_508600_idx")],
            },
        ),
    ]
