from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("board", "0016_username_normalized"),
    ]

    operations = [
        # Normalize any existing non-lowercase emails before adding constraint
        migrations.RunSQL(
            sql="UPDATE forum_users SET email = lower(email) WHERE email != lower(email);",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
                ALTER TABLE forum_users
                ADD CONSTRAINT email_lowercase CHECK (email = lower(email));
            """,
            reverse_sql="""
                ALTER TABLE forum_users DROP CONSTRAINT email_lowercase;
            """,
        ),
    ]
