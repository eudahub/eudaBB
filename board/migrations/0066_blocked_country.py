from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0065_post_topic_is_pending"),
    ]

    operations = [
        migrations.CreateModel(
            name="BlockedCountry",
            fields=[
                ("country_code", models.CharField(max_length=2, primary_key=True, serialize=False)),
                ("country_name", models.CharField(blank=True, default="", max_length=100)),
                (
                    "blocked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="board.user",
                    ),
                ),
                ("blocked_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "board_blocked_countries",
                "ordering": ["country_code"],
            },
        ),
    ]
