"""Populate User.email from sfinia_users_admin.db by username match.

Source: sfinia_users_admin.db → admin_users(username, email)
Only updates users with empty email — does not overwrite existing values.

Usage:
    python manage.py import_user_emails --db /path/to/sfinia_users_admin.db
"""
import sqlite3
from django.core.management.base import BaseCommand, CommandError
from board.models import User


class Command(BaseCommand):
    help = "Import plain emails from sfinia_users_admin.db into User.email."

    def add_arguments(self, parser):
        parser.add_argument(
            "--db",
            default="/home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_admin.db",
            help="Path to sfinia_users_admin.db",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing non-empty emails (default: skip)",
        )

    def handle(self, *args, **options):
        db_path = options["db"]
        overwrite = options["overwrite"]

        try:
            conn = sqlite3.connect(db_path)
        except Exception as exc:
            raise CommandError(f"Nie mozna otworzyc {db_path}: {exc}")

        rows = conn.execute(
            "SELECT username, email FROM admin_users WHERE email IS NOT NULL AND email != ''"
        ).fetchall()
        conn.close()

        self.stdout.write(f"Wczytano {len(rows)} rekordow z bazy.")

        updated = skipped_no_user = skipped_has_email = 0

        for username, raw_email in rows:
            email = raw_email.strip().lower()
            if not email:
                continue
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                skipped_no_user += 1
                continue

            if user.email and not overwrite:
                skipped_has_email += 1
                continue

            user.email = email
            user.save(update_fields=["email"])
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Zaktualizowano: {updated} | "
            f"Pominieto (juz ma email): {skipped_has_email} | "
            f"Pominieto (brak usera): {skipped_no_user}"
        ))
