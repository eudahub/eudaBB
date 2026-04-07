from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0030_topic_archive_topic_id_post_archive_post_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="likes_given_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="user",
            name="likes_received_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="post",
            name="like_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="PostLike",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("post", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="likes", to="board.post")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="given_post_likes", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "forum_post_likes",
                "unique_together": {("post", "user")},
            },
        ),
        migrations.AddIndex(
            model_name="postlike",
            index=models.Index(fields=["user", "created_at"], name="forum_post__user_id_604e7d_idx"),
        ),
        migrations.AddIndex(
            model_name="postlike",
            index=models.Index(fields=["post", "created_at"], name="forum_post__post_id_34afdd_idx"),
        ),
    ]
