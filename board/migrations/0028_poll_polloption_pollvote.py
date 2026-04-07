from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0027_siteconfig_search_snippet_chars"),
    ]

    operations = [
        migrations.CreateModel(
            name="Poll",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question", models.TextField()),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("is_closed", models.BooleanField(default=False)),
                ("allow_vote_change", models.BooleanField(default=False)),
                ("allow_multiple_choice", models.BooleanField(default=False)),
                ("is_archived_import", models.BooleanField(default=False)),
                ("total_votes", models.PositiveIntegerField(default=0)),
                ("source_visibility", models.IntegerField(default=0)),
                ("imported_results_text", models.TextField(blank=True, default="")),
                ("imported_fetched_at", models.DateTimeField(blank=True, null=True)),
                ("topic", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="poll", to="board.topic")),
            ],
            options={
                "db_table": "forum_polls",
            },
        ),
        migrations.CreateModel(
            name="PollOption",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("option_text", models.TextField()),
                ("vote_count", models.PositiveIntegerField(default=0)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("poll", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="options", to="board.poll")),
            ],
            options={
                "db_table": "forum_poll_options",
                "ordering": ["sort_order", "id"],
                "unique_together": {("poll", "sort_order")},
            },
        ),
        migrations.CreateModel(
            name="PollVote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("option", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="votes", to="board.polloption")),
                ("poll", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="votes", to="board.poll")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="poll_votes", to="board.user")),
            ],
            options={
                "db_table": "forum_poll_votes",
                "unique_together": {("poll", "user", "option")},
            },
        ),
        migrations.AddIndex(
            model_name="poll",
            index=models.Index(fields=["is_closed", "ends_at"], name="forum_polls_is_clos_7ce5f8_idx"),
        ),
        migrations.AddIndex(
            model_name="poll",
            index=models.Index(fields=["is_archived_import"], name="forum_polls_is_arch_7bf6e1_idx"),
        ),
        migrations.AddIndex(
            model_name="pollvote",
            index=models.Index(fields=["poll", "user"], name="forum_poll__poll_id_259b9e_idx"),
        ),
        migrations.AddIndex(
            model_name="pollvote",
            index=models.Index(fields=["option"], name="forum_poll__option__c25ca5_idx"),
        ),
    ]
