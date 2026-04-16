from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0067_alter_moderationwindow_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="registration_ip",
            field=models.GenericIPAddressField(
                blank=True, null=True,
                help_text="IP address used to register this account (for multi-account detection).",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="reg_ip_limit",
            field=models.BooleanField(
                default=True,
                help_text="Włącz limit rejestracji z tego samego IP.",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="reg_ip_window_hours",
            field=models.PositiveSmallIntegerField(
                default=6,
                help_text="Okno czasowe limitu rejestracji (godziny).",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="reg_ip_max_real",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="Max rejestracji realnych kont z jednego IP w oknie czasowym.",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="reg_ip_max_temp",
            field=models.PositiveSmallIntegerField(
                default=3,
                help_text="Max rejestracji kont tymczasowych z jednego IP w oknie czasowym.",
            ),
        ),
    ]
