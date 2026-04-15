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
from django.core.files.storage import default_storage

from board.models import User


class Command(BaseCommand):
    help = "Import ghost users from sfinia_import.db (plaintext emails)"

    def add_arguments(self, parser):
        parser.add_argument("import_db", help="Path to sfinia_import.db")
        parser.add_argument(
            "--avatars-dir",
            default="",
            help="Directory containing avatar files (e.g. /path/to/avatars)",
        )
        parser.add_argument(
            "--clear-ghosts",
            action="store_true",
            default=False,
            help="Delete existing ghost accounts before import (breaks post references!)",
        )
        parser.add_argument(
            "--need-rename",
            action="store_true",
            default=False,
            help=(
                "Use new_name column from users table as the target username. "
                "After import, updates [quote author=...] tags in posts for renamed users."
            ),
        )

    def handle(self, *args, **options):
        db_path = options["import_db"]

        if options["clear_ghosts"]:
            count, _ = User.objects.filter(password__startswith="!").delete()
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
            "user_id", "username", "email", "signature", "website", "location", "avatar_local_path",
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

        has_new_name = "new_name" in columns
        extra_name = (
            ", COALESCE(NULLIF(new_name,''), username) AS final_username"
            if has_new_name else
            ", username AS final_username"
        )
        rows = conn.execute(
            f"SELECT user_id, username, email, signature, website, location, avatar_local_path, "
            f"COALESCE(joined_at, '') AS joined_at, pass_hash, role{extra_name} "
            f"FROM users ORDER BY user_id"
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

        # --need-rename: also read new_name column directly from users table
        need_rename_map = {}
        if options.get("need_rename"):
            user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "new_name" in user_cols:
                nr_rows = conn.execute(
                    "SELECT username, new_name FROM users "
                    "WHERE new_name IS NOT NULL AND new_name != ''"
                ).fetchall()
                need_rename_map = {r["username"]: r["new_name"] for r in nr_rows}
                self.stdout.write(f"Wczytano {len(need_rename_map)} wpisów new_name.")
            else:
                self.stderr.write("Kolumna new_name nie istnieje w users — --need-rename ignorowane.")

        conn.close()

        avatars_dir = options["avatars_dir"]

        created = updated = avatars_set = renamed = 0
        for row in rows:
            # new_name (via final_username) takes precedence; fall back to alias map
            username = row["final_username"] or rename_map.get(row["username"], row["username"])
            if username != row["username"]:
                renamed += 1

            email = (row["email"] or "").strip().lower()
            pass_hash = row["pass_hash"]
            password  = pass_hash if pass_hash is not None else make_password(None)
            defaults = dict(
                is_active=True,
                email=email,
                signature=row["signature"] or "",
                website=row["website"]   or "",
                location=row["location"] or "",
                role=row["role"],
            )
            joined_str = (row["joined_at"] or "").strip()
            if joined_str:
                try:
                    defaults["date_joined"] = datetime.strptime(joined_str[:19], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={**defaults, "password": password},
            )

            if not was_created:
                if user.is_ghost():
                    for field, value in defaults.items():
                        setattr(user, field, value)
                    user.password = password
                    update_fields = list(defaults.keys()) + ["password"]
                else:
                    # Active user: only update profile metadata and role, not auth fields
                    for field in ("signature", "website", "location", "role"):
                        setattr(user, field, defaults[field])
                    update_fields = ["signature", "website", "location", "role"]

                local_path = row["avatar_local_path"] or ""
                if local_path and avatars_dir and not user.avatar:
                    filename = os.path.basename(local_path)
                    full_path = os.path.join(avatars_dir, filename)
                    if os.path.exists(full_path):
                        storage_name = user.avatar.field.upload_to + "/" + filename if user.avatar.field.upload_to else filename
                        if default_storage.exists(storage_name):
                            default_storage.delete(storage_name)
                        with open(full_path, "rb") as f:
                            user.avatar.save(filename, File(f), save=False)
                        update_fields.append("avatar")
                        avatars_set += 1

                user.save(update_fields=update_fields)
                updated += 1
                continue

            local_path = row["avatar_local_path"] or ""
            if local_path and avatars_dir:
                filename = os.path.basename(local_path)
                full_path = os.path.join(avatars_dir, filename)
                if os.path.exists(full_path):
                    storage_name = user.avatar.field.upload_to + "/" + filename if user.avatar.field.upload_to else filename
                    if default_storage.exists(storage_name):
                        default_storage.delete(storage_name)
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

        # --need-rename: update [quote author=...] in posts for renamed users.
        # At this point users already have new_name as username, so we only
        # rewrite BBCode in posts: old_name → new_name.
        if need_rename_map:
            from board.models import Post
            from board.user_rename import (
                _rewrite_named_quotes_only,
                _rewrite_enriched_quotes,
            )
            from board.quote_refs import rebuild_quote_references_for_posts
            from django.db import transaction

            # Build map: old_name → (new_name, frozenset of post_ids authored by new-user)
            rename_pairs = [
                (old, new)
                for old, new in need_rename_map.items()
                if old != new
            ]
            if rename_pairs:
                self.stdout.write(f"Przepisuję cytaty dla {len(rename_pairs)} przemianowanych użytkowników…")
                quotes_posts = quotes_tags = 0
                changed_post_ids = []
                batch = []

                # Precompute source post IDs per renamed user
                rename_pairs_with_ids = []
                for old_name, new_name_val in rename_pairs:
                    try:
                        user_obj = User.objects.get(username=new_name_val)
                        src_ids = frozenset(
                            Post.objects.filter(author=user_obj).values_list("pk", flat=True)
                        )
                    except User.DoesNotExist:
                        src_ids = frozenset()
                    rename_pairs_with_ids.append((old_name, new_name_val, src_ids))

                # Load all posts once; iterate and apply all renames
                all_posts = list(Post.objects.only("pk", "content_bbcode"))
                with transaction.atomic():
                    for post in all_posts:
                        content = post.content_bbcode
                        total_changed = 0
                        for old_name, new_name_val, src_ids in rename_pairs_with_ids:
                            content, c1 = _rewrite_named_quotes_only(content, old_name, new_name_val)
                            content, c2 = _rewrite_enriched_quotes(content, old_name, new_name_val, src_ids)  # noqa: E501
                            total_changed += c1 + c2
                        if total_changed:
                            post.content_bbcode = content
                            batch.append(post)
                            changed_post_ids.append(post.pk)
                            quotes_posts += 1
                            quotes_tags  += total_changed
                            if len(batch) >= 500:
                                Post.objects.bulk_update(batch, ["content_bbcode"])
                                batch.clear()
                    if batch:
                        Post.objects.bulk_update(batch, ["content_bbcode"])
                    if changed_post_ids:
                        rebuild_quote_references_for_posts(
                            Post.objects.filter(pk__in=changed_post_ids).only("pk", "content_bbcode")
                        )
                self.stdout.write(
                    f"Cytaty: przepisano {quotes_tags} tagów w {quotes_posts} postach."
                )
