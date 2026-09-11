import accounts.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_clitoken")]

    operations = [
        migrations.RemoveIndex(model_name="apitoken", name="accounts_ap_key_e6b164_idx"),
        migrations.RemoveIndex(model_name="clitoken", name="accounts_cl_key_has_508600_idx"),
        migrations.AlterField(
            model_name="apitoken",
            name="key",
            field=models.CharField(
                default=accounts.models._generate_token_key,
                max_length=64,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="clitoken",
            name="key_hash",
            field=models.CharField(max_length=64, unique=True),
        ),
    ]
