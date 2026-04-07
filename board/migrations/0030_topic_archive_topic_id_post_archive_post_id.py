from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0029_rename_forum_polls_is_clos_7ce5f8_idx_forum_polls_is_clos_05d262_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="topic",
            name="archive_topic_id",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="post",
            name="archive_post_id",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
    ]
