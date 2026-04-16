from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0074_forum_blog_of_siteconfig_post_edit_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfig",
            name="post_merge_minutes",
            field=models.PositiveSmallIntegerField(
                default=30,
                help_text="Okno scalania postów: jeśli user pisze ponownie w tym samym wątku i jego post jest ostatnim, treść dołącza się do poprzedniego (0 = wyłącz).",
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="post_merge_soft_kb",
            field=models.PositiveSmallIntegerField(
                default=20,
                help_text="Limit wielkości scalonego postu (kB). Jeśli połączona treść przekroczyłaby ten limit, tworzony jest nowy post.",
            ),
        ),
    ]
