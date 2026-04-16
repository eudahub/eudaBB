from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0070_notification"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="has_open_report",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="topic",
            name="open_report_count",
            field=models.PositiveSmallIntegerField(db_index=True, default=0),
        ),
        migrations.CreateModel(
            name="PostReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("reason", models.CharField(
                    choices=[("offtop", "Offtop"), ("rules", "Łamie regulamin")],
                    max_length=16,
                )),
                ("comment", models.CharField(blank=True, default="", max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("is_closed", models.BooleanField(db_index=True, default=False)),
                ("post", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="board_reports",
                    to="board.post",
                )),
                ("reporter", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="submitted_reports",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("resolved_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="resolved_reports",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"db_table": "forum_post_report", "ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="postreport",
            constraint=models.UniqueConstraint(
                fields=["post", "reporter"], name="unique_report_per_user"
            ),
        ),
    ]
