"""Populate User fields from sfinia_users_admin.db by username match.

Source: sfinia_users_admin.db → admin_users(username, email, signature, website, location)
Only updates users with empty fields — does not overwrite existing values unless --overwrite.

Usage:
    python manage.py import_user_emails --db /path/to/sfinia_users_admin.db
    python manage.py import_user_emails --overwrite
"""
import sqlite3
from django.core.management.base import BaseCommand, CommandError
from board.models import User


class Command(BaseCommand):
    help = "Import email, signature, location, website from sfinia_users_admin.db."

    def add_arguments(self, parser):
        parser.add_argument(
            "--db",
            default="/home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_admin.db",
            help="Path to sfinia_users_admin.db",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing non-empty values (default: skip)",
        )

    def handle(self, *args, **options):
        db_path = options["db"]
        overwrite = options["overwrite"]

        try:
            conn = sqlite3.connect(db_path)
        except Exception as exc:
            raise CommandError(f"Nie mozna otworzyc {db_path}: {exc}")

        rows = conn.execute(
            "SELECT username, email, signature, website, location FROM admin_users"
        ).fetchall()
        conn.close()

        self.stdout.write(f"Wczytano {len(rows)} rekordow z bazy.")

        updated = skipped_no_user = 0

        for username, raw_email, raw_sig, raw_web, raw_loc in rows:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                skipped_no_user += 1
                continue

            fields_changed = []

            email = (raw_email or "").strip().lower()
            if email and (not user.email or overwrite):
                user.email = email
                fields_changed.append("email")

            sig = (raw_sig or "").strip()
            if sig and (not user.signature or overwrite):
                user.signature = sig
                fields_changed.append("signature")

            web = (raw_web or "").strip()
            if web and (not user.website or overwrite):
                user.website = web
                fields_changed.append("website")

            loc = (raw_loc or "").strip()
            if loc and (not user.location or overwrite):
                user.location = loc
                fields_changed.append("location")

            if fields_changed:
                user.save(update_fields=fields_changed)
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Zaktualizowano: {updated} uzytkownikow | "
            f"Pominieto (brak usera): {skipped_no_user}"
        ))
