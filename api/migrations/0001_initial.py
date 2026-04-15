from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("board", "0062_spam_email"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FcmToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=255, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="fcm_tokens",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"db_table": "api_fcm_tokens"},
        ),
        migrations.CreateModel(
            name="PostReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason", models.CharField(blank=True, default="", max_length=500)),
                ("status", models.CharField(
                    choices=[("open", "Open"), ("resolved", "Resolved"), ("dismissed", "Dismissed")],
                    db_index=True,
                    default="open",
                    max_length=10,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("post", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="reports",
                    to="board.post",
                )),
                ("reporter", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="reports_made",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("resolved_by", models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="reports_resolved",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"db_table": "api_post_reports", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="postreport",
            index=models.Index(fields=["post"], name="api_postreport_post_idx"),
        ),
        migrations.AddIndex(
            model_name="postreport",
            index=models.Index(fields=["status", "-created_at"], name="api_postreport_status_idx"),
        ),
    ]
