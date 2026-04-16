import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0073_user_role_root"),
    ]

    operations = [
        migrations.AddField(
            model_name="forum",
            name="blog_of",
            field=models.ForeignKey(
                blank=True,
                help_text="Właściciel bloga — może edytować każdy post w tym dziale jak moderator.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="blog_forums",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="post_edit_minutes",
            field=models.PositiveSmallIntegerField(
                default=20,
                help_text="Ile minut zwykły użytkownik może edytować swój post (0 = bez limitu).",
            ),
        ),
    ]
