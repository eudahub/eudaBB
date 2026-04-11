"""
Dodaje kolumny "order" do tabel sections i forums w sfinia_full.db
i inicjuje je wartościami section_id / forum_id.

Bezpieczne do wielokrotnego uruchomienia — pomija jeśli kolumna już istnieje.

Użycie:
    python manage.py add_order_columns /ścieżka/do/sfinia_full.db
"""

import sqlite3

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Add 'order' columns to sections/forums in sfinia_full.db (idempotent)"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfinia_full.db")

    def handle(self, *args, **options):
        db_path = options["db_path"]
        try:
            conn = sqlite3.connect(db_path)
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        def has_column(table, column):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            return column in cols

        with conn:
            # sections."order"
            if not has_column("sections", "order"):
                conn.execute('ALTER TABLE sections ADD COLUMN "order" INTEGER NOT NULL DEFAULT 0')
                conn.execute('UPDATE sections SET "order" = section_id')
                self.stdout.write('sections."order" dodana i zainicjowana z section_id.')
            else:
                self.stdout.write('sections."order" już istnieje — pominięto.')

            # forums."order"
            if not has_column("forums", "order"):
                conn.execute('ALTER TABLE forums ADD COLUMN "order" INTEGER NOT NULL DEFAULT 0')
                conn.execute('UPDATE forums SET "order" = forum_id')
                self.stdout.write('forums."order" dodana i zainicjowana z forum_id.')
            else:
                self.stdout.write('forums."order" już istnieje — pominięto.')

        conn.close()
        self.stdout.write(self.style.SUCCESS("Gotowe."))
