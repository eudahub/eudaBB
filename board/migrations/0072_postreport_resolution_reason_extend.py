from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0071_post_report"),
    ]

    operations = [
        # Extend reason to free-text (Android may send anything)
        migrations.AlterField(
            model_name="postreport",
            name="reason",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        # Add resolution: resolved / dismissed
        migrations.AddField(
            model_name="postreport",
            name="resolution",
            field=models.CharField(
                blank=True,
                choices=[("resolved", "Rozwiązane"), ("dismissed", "Oddalone")],
                default="",
                max_length=10,
            ),
        ),
    ]
