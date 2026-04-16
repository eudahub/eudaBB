import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0075_siteconfig_post_merge"),
    ]

    operations = [
        migrations.AlterField(
            model_name="post",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="edited_posts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="post",
            name="merge_log",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
