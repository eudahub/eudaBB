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
from datetime import datetime, timezone

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

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        required_columns = {
            "user_id", "username", "email", "signature", "website", "location", "avatar",
        }
        if not required_columns.issubset(columns):
            conn.close()
            legacy_columns = {"has_email", "email_hash", "email_mask"}
            if legacy_columns.issubset(columns):
                raise CommandError(
                    "Detected legacy sfinia_import.db schema with has_email/email_hash/email_mask. "
                    "Plaintext emails cannot be recovered from that DB. "
                    "Rebuild it with: python manage.py build_import_db "
                    "/path/to/sfinia_users_admin.db /path/to/sfinia_users_real.db /path/to/sfinia_import.db"
                )
            missing = ", ".join(sorted(required_columns - columns))
            raise CommandError(
                f"Invalid import DB schema in {db_path}. Missing columns: {missing}"
            )

        rows = conn.execute(
            "SELECT user_id, username, email, signature, website, location, avatar, "
            "COALESCE(joined_at, '') AS joined_at "
            "FROM users ORDER BY user_id"
        ).fetchall()

        # Load rename map from username_aliases (action='rename')
        rename_map = {}
        try:
            alias_rows = conn.execute(
                "SELECT alias, new_name FROM username_aliases "
                "WHERE action='rename' AND new_name != ''"
            ).fetchall()
            rename_map = {r["alias"]: r["new_name"] for r in alias_rows}
            if rename_map:
                self.stdout.write(f"Wczytano {len(rename_map)} aliasów rename.")
        except Exception:
            pass  # table may not exist in older DBs

        conn.close()

        avatars_dir = options["avatars_dir"]

        created = updated = avatars_set = renamed = 0
        for row in rows:
            username = rename_map.get(row["username"], row["username"])
            if username != row["username"]:
                renamed += 1

            email = (row["email"] or "").strip().lower()
            defaults = dict(
                is_ghost=True,
                is_active=False,
                email=email,
                signature=row["signature"] or "",
                website=row["website"]   or "",
                location=row["location"] or "",
            )
            joined_str = (row["joined_at"] or "").strip()
            if joined_str:
                try:
                    defaults["date_joined"] = datetime.strptime(joined_str, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

            user, was_created = User.objects.get_or_create(
                username=username,
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
            + (f", przemianowano: {renamed}" if renamed else "")
            + (f", awatary: {avatars_set}" if avatars_set else "")
        ))
