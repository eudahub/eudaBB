from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0025_quotereference_ellipsis_count"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PostSearchIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField()),
                ("content_search_author", models.TextField(blank=True, default="")),
                ("content_search_author_normalized", models.TextField(blank=True, default="")),
                ("author", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="search_posts", to=settings.AUTH_USER_MODEL)),
                ("forum", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="search_posts", to="board.forum")),
                ("post", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="search_index", to="board.post")),
                ("topic", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="search_posts", to="board.topic")),
            ],
            options={
                "db_table": "forum_post_search",
            },
        ),
        migrations.AddIndex(
            model_name="postsearchindex",
            index=models.Index(fields=["forum", "created_at"], name="forum_post_s_forum_i_a3ec7a_idx"),
        ),
        migrations.AddIndex(
            model_name="postsearchindex",
            index=models.Index(fields=["topic", "created_at"], name="forum_post_s_topic_i_b23f1e_idx"),
        ),
        migrations.AddIndex(
            model_name="postsearchindex",
            index=models.Index(fields=["author"], name="forum_post_s_author__d38d66_idx"),
        ),
    ]
