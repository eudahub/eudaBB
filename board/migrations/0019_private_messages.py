from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0018_password_reset_code"),
    ]

    operations = [
        migrations.CreateModel(
            name="PrivateMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("subject", models.CharField(max_length=255)),
                ("content_compressed", models.BinaryField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("delivered_at", models.DateTimeField(
                    blank=True, null=True,
                    help_text="Set when recipient visits inbox. Null = still in sender's outbox.",
                )),
                ("sender", models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="sent_pms", to=settings.AUTH_USER_MODEL,
                )),
                ("recipient", models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="received_pms", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"db_table": "forum_pms", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="PrivateMessageBox",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("box_type", models.CharField(
                    choices=[("OUTBOX", "Outbox"), ("SENT", "Sent"), ("INBOX", "Inbox")],
                    max_length=6,
                )),
                ("is_read", models.BooleanField(default=False)),
                ("message", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="boxes", to="board.privatemessage",
                )),
                ("owner", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="pm_boxes", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"db_table": "forum_pm_boxes"},
        ),
        migrations.AddConstraint(
            model_name="privatemessagebox",
            constraint=models.UniqueConstraint(
                fields=["message", "owner"], name="unique_pm_box_per_user"
            ),
        ),
        migrations.AddIndex(
            model_name="privatemessagebox",
            index=models.Index(fields=["owner", "box_type"], name="pm_box_owner_type_idx"),
        ),
    ]
