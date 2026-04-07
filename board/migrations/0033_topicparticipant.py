from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0032_postsearchindex_link_youtube_flags"),
    ]

    operations = [
        migrations.CreateModel(
            name="TopicParticipant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("post_count", models.PositiveIntegerField(default=0)),
                ("last_post_at", models.DateTimeField(blank=True, null=True)),
                ("topic", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="participants", to="board.topic")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="topic_participations", to="board.user")),
            ],
            options={
                "db_table": "forum_topic_participants",
                "unique_together": {("topic", "user")},
            },
        ),
        migrations.AddIndex(
            model_name="topicparticipant",
            index=models.Index(fields=["topic", "post_count"], name="forum_topic_topic_id_b7e22c_idx"),
        ),
        migrations.AddIndex(
            model_name="topicparticipant",
            index=models.Index(fields=["user"], name="forum_topic_user_id_2dfcae_idx"),
        ),
    ]
