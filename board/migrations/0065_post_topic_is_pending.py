from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0064_moderation_window"),
    ]

    operations = [
        migrations.AddField(
            model_name="topic",
            name="is_pending",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text="Topic awaiting moderation — not visible on the forum.",
            ),
        ),
        migrations.AddField(
            model_name="post",
            name="is_pending",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text="Post awaiting moderation — not visible on the forum.",
            ),
        ),
    ]
