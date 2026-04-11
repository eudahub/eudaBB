from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0040_rename_forum_morph_suffix_lookup_idx_forum_morph_suffix__36918a_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="morphform",
            name="nom_form",
            field=models.CharField(default="", max_length=120),
        ),
    ]
