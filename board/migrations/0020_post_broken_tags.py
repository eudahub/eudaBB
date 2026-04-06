from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0019_private_messages"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="broken_tags",
            field=models.BooleanField(default=False),
        ),
    ]
