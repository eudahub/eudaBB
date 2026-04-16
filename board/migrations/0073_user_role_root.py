from django.db import migrations, models


def set_root_role(apps, schema_editor):
    User = apps.get_model("board", "User")
    User.objects.filter(is_root=True).update(role=3)


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0072_postreport_resolution_reason_extend"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.SmallIntegerField(
                choices=[(0, "Użytkownik"), (1, "Moderator"), (2, "Administrator"), (3, "Root")],
                db_index=True,
                default=0,
                help_text="0=użytkownik, 1=moderator, 2=administrator, 3=root. is_root blokuje pisanie/głosowanie.",
            ),
        ),
        migrations.RunPython(set_root_role, migrations.RunPython.noop),
    ]
