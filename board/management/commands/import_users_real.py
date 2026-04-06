"""
Import ghost accounts from sfinia_users_real.db.

Usage:
    python manage.py import_users_real /path/to/sfinia_users_real.db

Creates User records with is_ghost=True, is_active=False, unusable password.
Sets spam_class from the `spam` column (0=normal, 1=gray, 2=web; NULL treated as 0).
Skips users that already exist (by username).
"""

import sqlite3

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError

from board.models import User


class Command(BaseCommand):
    help = "Import ghost accounts from sfinia_users_real.db"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfinia_users_real.db")

    def handle(self, *args, **options):
        db_path = options["db_path"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        rows = conn.execute(
            "SELECT username, spam FROM users ORDER BY user_id"
        ).fetchall()
        conn.close()

        created = skipped = 0
        for row in rows:
            if User.objects.filter(username=row["username"]).exists():
                skipped += 1
                continue

            spam_class = row["spam"] if row["spam"] is not None else 0

            User.objects.create(
                username=row["username"],
                password=make_password(None),
                is_ghost=True,
                is_active=False,
                spam_class=spam_class,
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created: {created}, skipped (already exist): {skipped}"
        ))
