from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0026_postsearchindex"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfig",
            name="search_snippet_chars",
            field=models.PositiveIntegerField(default=800),
        ),
    ]
