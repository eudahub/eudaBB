from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0063_user_active_days"),
    ]

    operations = [
        migrations.CreateModel(
            name="ModerationWindow",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("start_hour", models.PositiveSmallIntegerField()),
                ("start_minute", models.PositiveSmallIntegerField(default=0)),
                ("end_hour", models.PositiveSmallIntegerField()),
                ("end_minute", models.PositiveSmallIntegerField(default=0)),
                ("day_from", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("day_to", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("timezone", models.CharField(default="Europe/Warsaw", max_length=64)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="moderation_windows",
                        to="board.user",
                    ),
                ),
            ],
            options={
                "db_table": "board_moderation_windows",
                "ordering": ["start_hour", "start_minute"],
            },
        ),
    ]
