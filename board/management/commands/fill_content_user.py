"""
Wypełnia content_user w sfinia_full.db dla postów gdzie content_user IS NULL.
Używa extract_content_user (zachowuje akapity) zamiast extract_author_search_text.

Dla postów need_repair_quotes=1:
  - jeśli cytaty niezbalansowane (≠ [quote]) → quote_status=4, nested_status bez zmian
  - jeśli zbalansowane                        → quote_status=NULL, nested_status=NULL

Nie modyfikuje kolumny content.

Użycie:
    python manage.py fill_content_user /ścieżka/sfinia_full.db
    python manage.py fill_content_user /ścieżka/sfinia_full.db --only-need-repair
"""

import re
import sqlite3

from django.core.management.base import BaseCommand, CommandError

from board.search_index import extract_content_user

CHUNK = 2_000
_OPEN_QUOTE_RE = re.compile(r"\[quote\b", re.IGNORECASE)
_CLOSE_QUOTE_RE = re.compile(r"\[/quote\]", re.IGNORECASE)


def _quotes_balanced(content: str) -> bool:
    opens = len(_OPEN_QUOTE_RE.findall(content))
    closes = len(_CLOSE_QUOTE_RE.findall(content))
    return opens == closes


class Command(BaseCommand):
    help = "Fill content_user in sfinia_full.db from content (strips quotes/code blocks)"

    def add_arguments(self, parser):
        parser.add_argument("db_path", help="Path to sfinia_full.db")
        parser.add_argument(
            "--only-need-repair",
            action="store_true",
            help="Process only posts with need_repair_quotes=1 (default: all with content_user IS NULL)",
        )

    def handle(self, *args, **options):
        db_path = options["db_path"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
        has_need_repair = "need_repair_quotes" in cols

        where = "content_user IS NULL"
        repair_mode = False
        if options["only_need_repair"] and has_need_repair:
            where = "need_repair_quotes = 1"
            repair_mode = True

        total = conn.execute(f"SELECT COUNT(*) FROM posts WHERE {where}").fetchone()[0]
        self.stdout.write(f"Postów do przetworzenia: {total:,}")

        updated = 0
        offset = 0
        while True:
            rows = conn.execute(
                f"SELECT post_id, content FROM posts WHERE {where} LIMIT {CHUNK} OFFSET {offset}"
            ).fetchall()
            if not rows:
                break

            batch = []
            for row in rows:
                content = row["content"] or ""
                content_user = extract_content_user(content)
                if repair_mode:
                    if _quotes_balanced(content):
                        # Zbalansowane — NULL = jeszcze nie przetworzone przez enrich_quotes
                        batch.append((content_user, len(content_user), None, None, row["post_id"]))
                    else:
                        # Niezbalansowane — quote_status=4
                        batch.append((content_user, len(content_user), 4, None, row["post_id"]))
                else:
                    batch.append((content_user, len(content_user), row["post_id"]))

            if repair_mode:
                conn.executemany(
                    "UPDATE posts SET content_user = ?, content_user_len = ?, "
                    "quote_status = ?, nested_status = ? WHERE post_id = ?",
                    batch,
                )
            else:
                conn.executemany(
                    "UPDATE posts SET content_user = ?, content_user_len = ? WHERE post_id = ?",
                    batch,
                )
            conn.commit()
            updated += len(batch)
            self.stdout.write(f"\r  {updated:,}/{total:,}...", ending="")
            self.stdout.flush()
            # W repair_mode WHERE nie zmienia się po UPDATE → przesuwamy OFFSET.
            # W trybie content_user IS NULL wiersze znikają z WHERE → OFFSET zawsze 0.
            if repair_mode:
                offset += CHUNK

        conn.close()
        self.stdout.write(f"\nGotowe. Zaktualizowano {updated:,} postów.")
