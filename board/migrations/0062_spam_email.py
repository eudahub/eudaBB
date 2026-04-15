from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0061_checklist_tags"),
    ]

    operations = [
        migrations.CreateModel(
            name="SpamEmail",
            fields=[
                ("email", models.EmailField(max_length=254, primary_key=True, serialize=False)),
                ("added_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "forum_spam_email",
            },
        ),
    ]
