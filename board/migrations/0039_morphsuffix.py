from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0038_rename_forum_morph_form_norm_idx_forum_morph_form_no_627cb1_idx_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="MorphSuffix",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ("suffix_len", models.SmallIntegerField()),
                ("suffix",     models.CharField(max_length=8)),
                ("lemma_norm", models.CharField(max_length=120)),
                ("family_id",  models.IntegerField()),
            ],
            options={
                "db_table": "forum_morph_suffix",
            },
        ),
        migrations.AlterUniqueTogether(
            name="morphsuffix",
            unique_together={("suffix_len", "suffix", "lemma_norm", "family_id")},
        ),
        migrations.AddIndex(
            model_name="morphsuffix",
            index=models.Index(fields=["suffix_len", "suffix"], name="forum_morph_suffix_lookup_idx"),
        ),
    ]
