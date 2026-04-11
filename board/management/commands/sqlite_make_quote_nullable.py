"""
Zmienia kolumny quote_status i nested_status w posts (sfinia_full.db)
z INTEGER NOT NULL DEFAULT 0 na INTEGER (dopuszcza NULL).

NULL = jeszcze nie przetworzone przez enrich_quotes
0    = przetworzono, brak cytatów
1    = wszystkie cytaty rozwiązane
2    = żaden cytat nie rozwiązany
3    = część rozwiązana
4    = niezbalansowane tagi (broken)

Po migracji schematu ustawia quote_status=NULL dla postów
need_repair_quotes=1 AND quote_status=0 (= importowane z admin,
content_user wypełniony, ale enrich_quotes jeszcze nie chodził).

Użycie:
    python manage.py sqlite_make_quote_nullable /ścieżka/sfinia_full.db
"""

import sqlite3

from django.core.management.base import BaseCommand, CommandError

NEW_SCHEMA = (
    'CREATE TABLE "posts_new" ('
    ' post_id INTEGER PRIMARY KEY,'
    ' topic_id INTEGER NOT NULL,'
    ' forum_id INTEGER,'
    ' topic_title TEXT,'
    ' author_name TEXT,'
    ' created_at TEXT,'
    ' subject TEXT,'
    ' content TEXT NOT NULL,'
    ' post_url TEXT,'
    ' post_order INTEGER,'
    ' refetch_state INTEGER NOT NULL DEFAULT 0,'
    ' content_user TEXT,'
    ' content_user_len INTEGER,'
    ' content_quotes TEXT,'
    ' quote_status INTEGER,'
    ' nested_status INTEGER,'
    ' need_repair_quotes INTEGER NOT NULL DEFAULT 0'
    ')'
)


class Command(BaseCommand):
    help = "Make quote_status/nested_status nullable in sfinia_full.db posts table"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfinia_full.db")

    def handle(self, *args, **options):
        db_path = options["db_path"]
        try:
            conn = sqlite3.connect(db_path)
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        # Sprawdź czy migracja już wykonana
        current = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'"
        ).fetchone()[0]
        if "NOT NULL DEFAULT 0" not in current or (
            "quote_status INTEGER NOT NULL" not in current
            and "nested_status INTEGER NOT NULL" not in current
        ):
            self.stdout.write("Kolumny już są nullable — nic do zrobienia.")
            conn.close()
            return

        self.stdout.write("Migracja schematu posts (quote_status/nested_status → nullable)...")

        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with conn:
                # 1. Utwórz nową tabelę
                conn.execute("DROP TABLE IF EXISTS posts_new")
                conn.execute(NEW_SCHEMA)

                # 2. Skopiuj dane
                conn.execute("""
                    INSERT INTO posts_new
                    SELECT post_id, topic_id, forum_id, topic_title, author_name,
                           created_at, subject, content, post_url, post_order,
                           refetch_state, content_user, content_user_len,
                           content_quotes, quote_status, nested_status, need_repair_quotes
                    FROM posts
                """)
                copied = conn.execute("SELECT COUNT(*) FROM posts_new").fetchone()[0]
                self.stdout.write(f"  Skopiowano {copied:,} wierszy.")

                # 3. Podmień tabelę
                conn.execute("DROP TABLE posts")
                conn.execute("ALTER TABLE posts_new RENAME TO posts")

                # 4. Odtwórz indeks
                conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts(topic_id)")
                self.stdout.write("  Indeks idx_posts_topic odtworzony.")

                # 5. Oznacz nieprzetworzone posty z admin jako NULL
                updated = conn.execute(
                    "UPDATE posts SET quote_status = NULL, nested_status = NULL"
                    " WHERE need_repair_quotes = 1 AND quote_status = 0"
                ).rowcount
                self.stdout.write(f"  quote_status → NULL dla {updated:,} postów (need_repair_quotes=1, quote_status=0).")

        finally:
            conn.execute("PRAGMA foreign_keys = ON")

        conn.close()
        self.stdout.write(self.style.SUCCESS("Gotowe."))
