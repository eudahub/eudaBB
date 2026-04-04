"""
Import users from sfinia_import.db (plaintext emails, lowercase).

Usage:
    python manage.py import_from_sfinia /path/to/sfinia_import.db [--avatars-dir DIR]

Uses update_or_create by username — existing users are updated in place,
so PKs never change and post author references remain valid.
Only ghost/inactive users are touched; active accounts are left alone.

--clear-ghosts  Delete existing ghost accounts before import (legacy, use with caution:
                breaks post author references if posts already imported).
"""

import os
import sqlite3

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.core.files import File

from board.models import User


class Command(BaseCommand):
    help = "Import ghost users from sfinia_import.db (plaintext emails)"

    def add_arguments(self, parser):
        parser.add_argument("import_db", help="Path to sfinia_import.db")
        parser.add_argument(
            "--avatars-dir",
            default="",
            help="Directory containing avatar files (e.g. /path/to/admin_avatars)",
        )
        parser.add_argument(
            "--clear-ghosts",
            action="store_true",
            default=False,
            help="Delete existing ghost accounts before import (breaks post references!)",
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
            "SELECT user_id, username, email, signature, website, location, avatar "
            "FROM users ORDER BY user_id"
        ).fetchall()
        conn.close()

        avatars_dir = options["avatars_dir"]

        created = updated = avatars_set = 0
        for row in rows:
            email = (row["email"] or "").strip().lower()
            defaults = dict(
                is_ghost=True,
                is_active=False,
                email=email,
                signature=row["signature"] or "",
                website=row["website"]   or "",
                location=row["location"] or "",
            )

            user, was_created = User.objects.get_or_create(
                username=row["username"],
                defaults={**defaults, "password": make_password(None)},
            )

            if not was_created:
                if user.is_ghost:
                    for field, value in defaults.items():
                        setattr(user, field, value)
                    update_fields = list(defaults.keys())
                else:
                    # Active user: only update profile metadata, not auth fields
                    for field in ("signature", "website", "location"):
                        setattr(user, field, defaults[field])
                    update_fields = ["signature", "website", "location"]

                local_path = row["avatar"] or ""
                if local_path and avatars_dir and not user.avatar:
                    filename = os.path.basename(local_path)
                    full_path = os.path.join(avatars_dir, filename)
                    if os.path.exists(full_path):
                        with open(full_path, "rb") as f:
                            user.avatar.save(filename, File(f), save=False)
                        update_fields.append("avatar")
                        avatars_set += 1

                user.save(update_fields=update_fields)
                updated += 1
                continue

            local_path = row["avatar"] or ""
            if local_path and avatars_dir:
                filename = os.path.basename(local_path)
                full_path = os.path.join(avatars_dir, filename)
                if os.path.exists(full_path):
                    with open(full_path, "rb") as f:
                        user.avatar.save(filename, File(f), save=False)
                    user.save(update_fields=["avatar"])
                    avatars_set += 1

            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Utworzono: {created}, zaktualizowano: {updated}"
            + (f", awatary: {avatars_set}" if avatars_set else "")
        ))
