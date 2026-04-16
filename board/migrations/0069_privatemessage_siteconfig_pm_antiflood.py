from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0068_user_registration_ip_siteconfig_reg_ip_limit"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfig",
            name="pm_min_active_days",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="Minimalny active_days aby móc wysyłać PM (0 = brak bramki).",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="pm_max_burst",
            field=models.PositiveSmallIntegerField(
                default=2,
                help_text="Max nieprzerwanych PM do tej samej osoby bez odpowiedzi.",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="pm_cold_reset_hours",
            field=models.PositiveSmallIntegerField(
                default=24,
                help_text="Po ilu godzinach bez odpowiedzi licznik burst się resetuje.",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="pm_new_recipients_per_day",
            field=models.PositiveSmallIntegerField(
                default=5,
                help_text="Max nowych rozmówców (bez historii) dziennie.",
            ),
        ),
    ]
