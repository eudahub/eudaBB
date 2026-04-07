from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0031_postlike_and_like_counters"),
    ]

    operations = [
        migrations.AddField(
            model_name="postsearchindex",
            name="has_link",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="postsearchindex",
            name="has_youtube",
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name="postsearchindex",
            index=models.Index(fields=["has_link"], name="forum_post__has_lin_44be58_idx"),
        ),
        migrations.AddIndex(
            model_name="postsearchindex",
            index=models.Index(fields=["has_youtube"], name="forum_post__has_you_6a6da5_idx"),
        ),
    ]
