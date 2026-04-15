from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0060_checklistitem_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklist",
            name="allowed_tags",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="checklistitem",
            name="tag",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
    ]
