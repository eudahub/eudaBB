# Generated manually for quote reference index

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0021_siteconfig"),
    ]

    operations = [
        migrations.CreateModel(
            name="QuoteReference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quote_type", models.CharField(choices=[("quote", "Quote"), ("fquote", "Foreign quote")], max_length=6)),
                ("quoted_username", models.CharField(blank=True, default="", max_length=150)),
                ("depth", models.PositiveSmallIntegerField(default=1)),
                ("quote_index", models.PositiveIntegerField(default=0)),
                ("post", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="quote_references", to="board.post")),
                ("source_post", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="quoted_by", to="board.post")),
            ],
            options={
                "db_table": "forum_quote_refs",
                "unique_together": {("post", "quote_index")},
            },
        ),
        migrations.AddIndex(
            model_name="quotereference",
            index=models.Index(fields=["source_post"], name="forum_quote_source__97bdf8_idx"),
        ),
        migrations.AddIndex(
            model_name="quotereference",
            index=models.Index(fields=["quoted_username"], name="forum_quote_quoted__7e9bdf_idx"),
        ),
        migrations.AddIndex(
            model_name="quotereference",
            index=models.Index(fields=["post", "depth"], name="forum_quote_post_id_d2b179_idx"),
        ),
    ]
