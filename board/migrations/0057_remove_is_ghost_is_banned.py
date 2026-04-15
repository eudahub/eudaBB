from django.db import migrations


def ghost_to_active(apps, schema_editor):
    """Ghosts were is_active=False; new design: ghost = no usable password, is_active=True."""
    User = apps.get_model("board", "User")
    User.objects.filter(is_ghost=True).update(is_active=True)


def banned_to_inactive(apps, schema_editor):
    """is_banned=True maps to is_active=False in new design."""
    User = apps.get_model("board", "User")
    User.objects.filter(is_banned=True).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0056_remove_show_switch_link"),
    ]

    operations = [
        # 1. Data: ghosts become active (login blocked by missing password, not by flag)
        migrations.RunPython(ghost_to_active, migrations.RunPython.noop),
        # 2. Data: banned users become inactive
        migrations.RunPython(banned_to_inactive, migrations.RunPython.noop),
        # 3. Schema: drop is_ghost
        migrations.RemoveField(model_name="user", name="is_ghost"),
        # 4. Schema: drop is_banned
        migrations.RemoveField(model_name="user", name="is_banned"),
    ]
