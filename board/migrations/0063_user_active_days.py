from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0062_spam_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="active_days",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Number of distinct UTC days on which the user posted at least one post.",
            ),
        ),
    ]
