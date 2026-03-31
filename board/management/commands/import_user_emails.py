"""
One-time import of emails from sfinia_users_admin.db into ghost accounts.

Usage:
    python manage.py import_user_emails /path/to/sfinia_users_admin.db

For each ghost user whose username matches a record in admin_users:
- Computes Argon2 hash of the email (slow, ~1s per user)
- Stores email_hash and email_mask on the User record

Progress is printed every 10 users. Safe to re-run — skips users
that already have email_hash set.
"""

import sqlite3

from django.core.management.base import BaseCommand, CommandError

from board.email_utils import hash_email, mask_email
from board.models import User


class Command(BaseCommand):
    help = "Import email hashes from sfinia_users_admin.db into ghost accounts"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfinia_users_admin.db")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-hash even if email_hash already set",
        )

    def handle(self, *args, **options):
        db_path = options["db_path"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        rows = conn.execute(
            "SELECT username, email FROM admin_users "
            "WHERE email IS NOT NULL AND email <> ''"
        ).fetchall()
        conn.close()

        total = len(rows)
        done = skipped = missing = 0

        self.stdout.write(f"Found {total} users with email in source DB.")
        self.stdout.write("Hashing with Argon2 (~1s per user)...\n")

        for i, row in enumerate(rows, 1):
            try:
                user = User.objects.get(username=row["username"], is_ghost=True)
            except User.DoesNotExist:
                missing += 1
                continue

            if user.email_hash and not options["force"]:
                skipped += 1
                continue

            user.email_hash = hash_email(row["email"])
            user.email_mask = mask_email(row["email"])
            user.save(update_fields=["email_hash", "email_mask"])
            done += 1

            if i % 10 == 0:
                self.stdout.write(f"  {i}/{total} processed...")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Hashed: {done}, skipped (already set): {skipped}, "
            f"not found as ghost: {missing}"
        ))
