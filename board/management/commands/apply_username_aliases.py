"""Apply username alias decisions from sfinia_users_real.db.

Actions:
  merge  — re-assign all posts/topics from alias user to canonical user,
            then delete alias user. Used when two accounts belong to same person.
  rename — rename alias user to new_name. Used when two different people
            collide on the same normalized username.

Usage:
    python manage.py apply_username_aliases [--dry-run]
"""
import sqlite3
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction
from board.models import User, Post, Topic
from board.user_rename import rename_user_and_update_quotes

DB_PATH = "/home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_real.db"


class Command(BaseCommand):
    help = "Apply username merge/rename decisions from sfinia_users_real.db."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would happen without changing anything.")
        parser.add_argument("--db", default=DB_PATH)

    def handle(self, *args, **options):
        dry = options["dry_run"]
        if dry:
            self.stdout.write("=== DRY RUN — brak zmian ===\n")

        conn = sqlite3.connect(options["db"])
        rows = conn.execute(
            "SELECT alias, action, canonical, new_name, note FROM username_aliases ORDER BY alias"
        ).fetchall()
        conn.close()

        for alias, action, canonical, new_name, note in rows:
            self.stdout.write(f"\n[{action.upper()}] {alias!r}")
            if note:
                self.stdout.write(f"  Powód: {note}")

            if action == "merge":
                self._merge(alias, canonical, dry)
            elif action == "rename":
                self._rename(alias, new_name, dry)
            else:
                self.stderr.write(f"  Nieznana akcja: {action!r}")

    def _merge(self, alias_name, canonical_name, dry):
        try:
            alias_user = User.objects.get(username=alias_name)
        except User.DoesNotExist:
            self.stdout.write(f"  POMINIĘTO — brak usera {alias_name!r}")
            return
        try:
            canonical_user = User.objects.get(username=canonical_name)
        except User.DoesNotExist:
            self.stdout.write(f"  BŁĄD — brak canonical {canonical_name!r}")
            return

        posts = Post.objects.filter(author=alias_user).count()
        topics = Topic.objects.filter(author=alias_user).count()
        self.stdout.write(f"  {alias_name!r} (id={alias_user.id}) → {canonical_name!r} (id={canonical_user.id})")
        self.stdout.write(f"  Przeniesienie: {posts} postów, {topics} wątków")

        if not dry:
            with transaction.atomic():
                Post.objects.filter(author=alias_user).update(author=canonical_user)
                Topic.objects.filter(author=alias_user).update(author=canonical_user)
                # Update post count on canonical
                canonical_user.post_count = Post.objects.filter(author=canonical_user).count()
                canonical_user.save(update_fields=["post_count"])
                alias_user.delete()
            self.stdout.write(self.style.SUCCESS(f"  OK — scalono i usunięto {alias_name!r}"))

    def _rename(self, alias_name, new_name, dry):
        try:
            alias_user = User.objects.get(username=alias_name)
        except User.DoesNotExist:
            self.stdout.write(f"  POMINIĘTO — brak usera {alias_name!r}")
            return
        posts = Post.objects.filter(author=alias_user).count()
        self.stdout.write(f"  {alias_name!r} (id={alias_user.id}, {posts} postów) → {new_name!r}")
        if not dry:
            try:
                result = rename_user_and_update_quotes(alias_user, new_name)
            except ValidationError as exc:
                self.stdout.write(f"  BŁĄD — {'; '.join(exc.messages)}")
                return
            self.stdout.write(self.style.SUCCESS(
                f"  OK — zmieniono nazwę, poprawiono {result['tags_changed']} tagów quote "
                f"w {result['posts_changed']} postach"
            ))
