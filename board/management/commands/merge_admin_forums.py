"""
Scala tabelę forums_admin z forums w sfinia_full.db.

Kroki:
  1. Sprawdza czy forum_id są rozłączne (bezpieczeństwo).
  2. Dodaje kolumny section_id=7 i "order" (10,20,30...) do forums_admin.
  3. Kopiuje wszystkie wiersze z forums_admin do forums.
  4. Opcjonalnie usuwa forums_admin (--drop).

Bezpieczne do ponownego uruchomienia — pomija wiersze, które już są w forums.

Użycie:
    python manage.py merge_admin_forums /ścieżka/sfinia_full.db
    python manage.py merge_admin_forums /ścieżka/sfinia_full.db --drop
"""

import sqlite3

from django.core.management.base import BaseCommand, CommandError

SECTION_ID = 7   # Biuro
ORDER_START = 10
ORDER_STEP  = 1


class Command(BaseCommand):
    help = "Merge forums_admin into forums in sfinia_full.db"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfinia_full.db")
        parser.add_argument(
            "--drop",
            action="store_true",
            help="Drop forums_admin table after successful merge",
        )

    def handle(self, *args, **options):
        db_path = options["db_path"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        # 1. Sprawdź rozłączność
        overlap = conn.execute(
            "SELECT COUNT(*) FROM forums f "
            "JOIN forums_admin fa ON CAST(fa.forum_id AS INTEGER) = f.forum_id"
        ).fetchone()[0]
        if overlap:
            conn.close()
            raise CommandError(
                f"Kolizja! {overlap} forum_id występuje w obu tabelach. Przerywam."
            )
        self.stdout.write(f"Rozłączność OK (0 kolizji).")

        def has_column(table, column):
            return column in {
                r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }

        with conn:
            # 2a. Dodaj section_id do forums_admin
            if not has_column("forums_admin", "section_id"):
                conn.execute(
                    f"ALTER TABLE forums_admin ADD COLUMN section_id INTEGER NOT NULL DEFAULT {SECTION_ID}"
                )
                self.stdout.write(f"forums_admin.section_id dodana (={SECTION_ID}).")
            else:
                conn.execute(f"UPDATE forums_admin SET section_id = {SECTION_ID}")
                self.stdout.write(f"forums_admin.section_id zaktualizowana (={SECTION_ID}).")

            # 2b. Dodaj "order" do forums_admin i przypisz 10,20,30...
            if not has_column("forums_admin", "order"):
                conn.execute(
                    'ALTER TABLE forums_admin ADD COLUMN "order" INTEGER NOT NULL DEFAULT 0'
                )
                self.stdout.write('forums_admin."order" dodana.')

            # Przypisz order: 10, 20, 30, ... według ROWID
            conn.execute("""
                UPDATE forums_admin
                SET "order" = (
                    SELECT (COUNT(*) - 1) * ? + ?
                    FROM forums_admin fa2
                    WHERE fa2.rowid <= forums_admin.rowid
                )
            """, (ORDER_STEP, ORDER_START))
            self.stdout.write(
                f'forums_admin."order" przypisana ({ORDER_START},{ORDER_START+ORDER_STEP},...)'
            )

            # 3. Wstaw do forums (pomijaj już istniejące forum_id)
            conn.execute("""
                INSERT OR IGNORE INTO forums (
                    forum_id, parent_forum_id, visibility, title, description,
                    url, topic_count, post_count, moderator_names,
                    last_post_at, last_post_author, last_post_author_url, last_post_url,
                    section_id, "order"
                )
                SELECT
                    CAST(forum_id AS INTEGER),
                    NULLIF(CAST(NULLIF(parent_forum_id, '') AS INTEGER), 0),
                    visibility,
                    title,
                    description,
                    url,
                    topic_count,
                    post_count,
                    moderator_names,
                    last_post_at,
                    last_post_author,
                    last_post_author_url,
                    last_post_url,
                    section_id,
                    "order"
                FROM forums_admin
            """)
            inserted = conn.execute(
                "SELECT COUNT(*) FROM forums f "
                "JOIN forums_admin fa ON CAST(fa.forum_id AS INTEGER) = f.forum_id"
            ).fetchone()[0]
            self.stdout.write(f"Wstawiono {inserted} forów z forums_admin do forums.")

            # 4. Opcjonalne usunięcie forums_admin
            if options["drop"]:
                conn.execute("DROP TABLE forums_admin")
                self.stdout.write("Tabela forums_admin usunięta.")

        if not options["drop"]:
            total = conn.execute("SELECT COUNT(*) FROM forums").fetchone()[0]
            conn.close()
            self.stdout.write(self.style.SUCCESS(f"Gotowe. forums ma teraz {total} forów."))
        else:
            conn.close()
            self.stdout.write(self.style.SUCCESS("Gotowe."))
