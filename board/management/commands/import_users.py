"""
Import ghost accounts from sfinia_users_admin.db.

Usage:
    python manage.py import_users /path/to/sfinia_users_admin.db

Creates User records with is_ghost=True, is_active=False, unusable password.
Skips users that already exist (by username).
Run import_user_emails afterwards to hash their emails.
"""

import sqlite3

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError

from board.models import User


class Command(BaseCommand):
    help = "Import ghost accounts from sfinia_users_admin.db"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfinia_users_admin.db")

    def handle(self, *args, **options):
        db_path = options["db_path"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        rows = conn.execute(
            "SELECT user_id, username, signature, website, location "
            "FROM admin_users ORDER BY user_id"
        ).fetchall()
        conn.close()

        created = skipped = 0
        for row in rows:
            if User.objects.filter(username=row["username"]).exists():
                skipped += 1
                continue

            User.objects.create(
                username=row["username"],
                password=make_password(None),  # unusable password
                is_ghost=True,
                is_active=False,
                signature=row["signature"] or "",
                website=row["website"] or "",
                location=row["location"] or "",
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created: {created}, skipped (already exist): {skipped}"
        ))
