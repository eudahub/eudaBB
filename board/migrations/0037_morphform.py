from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0036_topic_last_post_at_gray_topic_last_post_at_normal_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="MorphForm",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ("form_norm",  models.CharField(max_length=120)),
                ("lemma_norm", models.CharField(max_length=120)),
                ("family_id",  models.IntegerField()),
            ],
            options={
                "db_table": "forum_morph_form",
            },
        ),
        migrations.AlterUniqueTogether(
            name="morphform",
            unique_together={("form_norm", "lemma_norm", "family_id")},
        ),
        migrations.AddIndex(
            model_name="morphform",
            index=models.Index(fields=["form_norm"], name="forum_morph_form_norm_idx"),
        ),
        migrations.AddIndex(
            model_name="morphform",
            index=models.Index(fields=["lemma_norm", "family_id"], name="forum_morph_lemma_fam_idx"),
        ),
    ]
