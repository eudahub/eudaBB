from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0069_privatemessage_siteconfig_pm_antiflood"),
    ]

    operations = [
        migrations.CreateModel(
            name="Notification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("notif_type", models.CharField(
                    choices=[
                        ("quote_reply",        "Odpowiedź z cytatem"),
                        ("post_liked",         "Plus za post"),
                        ("post_unliked",       "Cofnięcie plusa"),
                        ("pending_queue",      "Kolejka oczekujących"),
                        ("post_reported",      "Zgłoszony post"),
                        ("pm_reported",        "Zgłoszona PM"),
                        ("report_closed_post", "Zamknięto zgłoszenie postu"),
                        ("report_closed_pm",   "Zamknięto zgłoszenie PM"),
                    ],
                    max_length=24,
                )),
                ("is_read", models.BooleanField(db_index=True, default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="sent_notifications",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("post", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="+",
                    to="board.post",
                )),
                ("pm", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="+",
                    to="board.privatemessage",
                )),
                ("recipient", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="notifications",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "db_table": "forum_notification",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(
                fields=["recipient", "is_read", "created_at"],
                name="notif_recipient_unread_idx",
            ),
        ),
    ]
