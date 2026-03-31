from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="forum",
            name="section",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="forums",
                to="board.section",
            ),
        ),
    ]
