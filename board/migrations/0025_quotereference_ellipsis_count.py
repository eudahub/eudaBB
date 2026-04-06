from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0024_rename_forum_quote_source__97bdf8_idx_forum_quote_source__6f994d_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="quotereference",
            name="ellipsis_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
