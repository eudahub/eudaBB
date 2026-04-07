from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0033_topicparticipant"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="mark_all_read_at",
            field=models.DateTimeField(default=timezone.now, help_text="Global baseline: everything older than this is treated as read."),
        ),
        migrations.CreateModel(
            name="TopicReadState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_read_post_order", models.PositiveIntegerField(default=0)),
                ("last_read_at", models.DateTimeField(default=timezone.now)),
                ("topic", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="read_states", to="board.topic")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="topic_read_states", to="board.user")),
            ],
            options={
                "db_table": "forum_topic_read_states",
                "unique_together": {("user", "topic")},
            },
        ),
        migrations.AddIndex(
            model_name="topicreadstate",
            index=models.Index(fields=["user", "last_read_at"], name="forum_topic_user_id_114d70_idx"),
        ),
        migrations.AddIndex(
            model_name="topicreadstate",
            index=models.Index(fields=["topic"], name="forum_topic_topic_id_0c3eca_idx"),
        ),
    ]
