"""
Import spam_class from sfinia_users_real.db.

Maps archive spam column:  0 → NORMAL, 1 → GRAY, 2 → WEB
Matches users by username. Skips unknown usernames.

Usage:
    python manage.py import_spam_classes /path/to/sfinia_users_real.db
"""

import sqlite3

from django.core.management.base import BaseCommand, CommandError

from board.models import User


class Command(BaseCommand):
    help = "Import spam_class from sfinia_users_real.db (0=normal, 1=gray, 2=web)"

    def add_arguments(self, parser):
        parser.add_argument("real_db", help="Path to sfinia_users_real.db")

    def handle(self, *args, **options):
        try:
            conn = sqlite3.connect(options["real_db"])
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {options['real_db']}: {e}")

        rows = conn.execute(
            "SELECT username, spam FROM users WHERE spam != 0 ORDER BY spam"
        ).fetchall()
        conn.close()

        user_map = {u.username: u for u in User.objects.only("id", "username", "spam_class")}

        updated = skipped = 0
        for row in rows:
            user = user_map.get(row["username"])
            if user is None:
                skipped += 1
                continue
            user.spam_class = row["spam"]
            updated += 1

        User.objects.bulk_update(
            [u for u in user_map.values() if u.spam_class != User.SpamClass.NORMAL],
            ["spam_class"],
            batch_size=500,
        )

        by_class = {}
        for row in rows:
            by_class[row["spam"]] = by_class.get(row["spam"], 0) + 1

        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Zaktualizowano: {updated}, pominięto (brak w DB): {skipped}\n"
            f"  gray (1): {by_class.get(1, 0)}, web (2): {by_class.get(2, 0)}"
        ))
