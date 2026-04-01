"""
Import users from sfinia_import.db (pre-hashed, no plaintext emails).

Usage:
    python manage.py import_from_sfinia /path/to/sfinia_import.db [--clear-ghosts]

--clear-ghosts  Delete existing ghost accounts before import (default: False).
                Safe: only removes is_ghost=True users, never root or active accounts.

Creates User records with is_ghost=True, is_active=False.
Skips users whose username already exists in the DB.
"""

import sqlite3

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError

from board.models import User


class Command(BaseCommand):
    help = "Import ghost users from sfinia_import.db (pre-hashed emails)"

    def add_arguments(self, parser):
        parser.add_argument("import_db", help="Path to sfinia_import.db")
        parser.add_argument(
            "--clear-ghosts",
            action="store_true",
            default=False,
            help="Delete existing ghost accounts before import",
        )

    def handle(self, *args, **options):
        db_path = options["import_db"]

        if options["clear_ghosts"]:
            count, _ = User.objects.filter(is_ghost=True).delete()
            self.stdout.write(f"Usunięto {count} starych duchów.")

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        rows = conn.execute(
            "SELECT username, has_email, email_hash, email_mask, "
            "       signature, website, location "
            "FROM users ORDER BY user_id"
        ).fetchall()
        conn.close()

        existing = set(User.objects.values_list("username", flat=True))

        created = skipped = 0
        for row in rows:
            if row["username"] in existing:
                skipped += 1
                continue

            User.objects.create(
                username=row["username"],
                password=make_password(None),       # unusable password
                is_ghost=True,
                is_active=False,
                email="",
                email_hash=row["email_hash"] or "",
                email_mask=row["email_mask"] or "",
                signature=row["signature"] or "",
                website=row["website"]   or "",
                location=row["location"] or "",
            )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Utworzono: {created}, pominięto (już istnieją): {skipped}"
        ))
