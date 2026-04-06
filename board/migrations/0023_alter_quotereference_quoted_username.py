# Generated manually to allow long quote labels

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0022_quotereference"),
    ]

    operations = [
        migrations.AlterField(
            model_name="quotereference",
            name="quoted_username",
            field=models.TextField(blank=True, default=""),
        ),
    ]
