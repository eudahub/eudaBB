"""
Build a sanitized import database from sfinia source DBs.

Usage:
    python manage.py build_import_db \\
        /path/to/sfinia_users_admin.db \\
        /path/to/sfinia_users_real.db \\
        /path/to/output.db

Sources:
  sfinia_users_admin.db — 1036 logged-in users with emails, signatures, etc.
  sfinia_users_real.db  — 3755 all users (logged + guests), spam stats only

Output DB schema (users table):
  user_id, username, email (lowercase plaintext),
  signature, website, location, avatar

Also copies `username_aliases` from sfinia_users_real.db when present.
"""

import sqlite3

from django.core.management.base import BaseCommand


CREATE_SQL = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS username_aliases;
CREATE TABLE users (
    user_id   INTEGER,
    username  TEXT NOT NULL,
    email     TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL DEFAULT '',
    website   TEXT NOT NULL DEFAULT '',
    location  TEXT NOT NULL DEFAULT '',
    avatar    TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_username ON users(username);

CREATE TABLE username_aliases (
    alias       TEXT PRIMARY KEY,
    action      TEXT NOT NULL CHECK(action IN ('merge', 'rename')),
    canonical   TEXT NOT NULL DEFAULT '',
    new_name    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);
"""


class Command(BaseCommand):
    help = "Build import DB with plaintext emails (normalized to lowercase)"

    def add_arguments(self, parser):
        parser.add_argument("admin_db",  help="Path to sfinia_users_admin.db")
        parser.add_argument("real_db",   help="Path to sfinia_users_real.db")
        parser.add_argument("output_db", help="Path to output .db file")
        parser.add_argument(
            "--public-db",
            default="",
            help="Path to sfinia_users_public.db (provides joined_at dates)",
        )

    def handle(self, *args, **options):
        admin_path  = options["admin_db"]
        real_path   = options["real_db"]
        output_path = options["output_db"]

        admin_conn = sqlite3.connect(admin_path)
        admin_conn.row_factory = sqlite3.Row
        admin_columns = {
            row["name"]
            for row in admin_conn.execute("PRAGMA table_info(admin_users)").fetchall()
        }
        if "avatar_local_path" not in admin_columns:
            admin_conn.close()
            raise RuntimeError(
                "Brak kolumny avatar_local_path w admin_users. "
                "Najpierw odśwież bazę sfinia_users_admin.db narzędziem archivera."
            )

        admin_rows = {
            r["username"]: r
            for r in admin_conn.execute(
                "SELECT user_id, username, email, signature, website, location, "
                "COALESCE(avatar_local_path, '') AS avatar "
                "FROM admin_users ORDER BY user_id"
            ).fetchall()
        }
        admin_conn.close()

        real_conn = sqlite3.connect(real_path)
        real_conn.row_factory = sqlite3.Row
        real_rows = real_conn.execute(
            "SELECT user_id, username FROM users ORDER BY user_id"
        ).fetchall()
        alias_rows = []
        has_alias_table = real_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='username_aliases'"
        ).fetchone()
        if has_alias_table:
            alias_rows = real_conn.execute(
                "SELECT alias, action, canonical, new_name, note, created_at "
                "FROM username_aliases ORDER BY alias"
            ).fetchall()
        real_conn.close()

        # Load joined_at from public DB (username → ISO date string)
        public_joined = {}
        public_db_path = options.get("public_db", "")
        if public_db_path:
            try:
                pub_conn = sqlite3.connect(public_db_path)
                pub_conn.row_factory = sqlite3.Row
                pub_rows = pub_conn.execute(
                    "SELECT username, joined_at FROM users WHERE joined_at != ''"
                ).fetchall()
                pub_conn.close()
                pl_months = {
                    "Sty": "01", "Lut": "02", "Mar": "03", "Kwi": "04",
                    "Maj": "05", "Cze": "06", "Lip": "07", "Sie": "08",
                    "Wrz": "09", "Paź": "10", "Paz": "10", "Lis": "11", "Gru": "12",
                }
                for r in pub_rows:
                    raw = (r["joined_at"] or "").strip()
                    parts = raw.split()
                    if len(parts) == 3:
                        day, mon, year = parts
                        m = pl_months.get(mon)
                        if m:
                            public_joined[r["username"]] = f"{year}-{m}-{day.zfill(2)}"
                self.stdout.write(f"Wczytano {len(public_joined)} dat rejestracji z public DB.")
            except Exception as e:
                self.stderr.write(f"Ostrzeżenie: nie wczytano public DB ({e})")

        # Merge: admin users first (have emails), then real-only users
        seen = set()
        merged = []
        for r in admin_rows.values():
            merged.append(("admin", r))
            seen.add(r["username"])
        for r in real_rows:
            if r["username"] not in seen:
                merged.append(("real", r))
                seen.add(r["username"])

        with_email = sum(1 for t, r in merged if t == "admin" and r["email"])
        self.stdout.write(
            f"Łącznie: {len(merged)} userów (z emailem: {with_email})"
        )

        out_conn = sqlite3.connect(output_path)
        out_conn.executescript(CREATE_SQL)

        for tag, row in merged:
            is_admin = (tag == "admin")
            raw_email = row["email"].strip().lower() if is_admin and row["email"] else ""

            out_conn.execute(
                "INSERT INTO users (user_id, username, email, signature, website, location, avatar, joined_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["user_id"] if "user_id" in row.keys() else None,
                    row["username"],
                    raw_email,
                    (row["signature"] or "") if is_admin else "",
                    (row["website"]   or "") if is_admin else "",
                    (row["location"]  or "") if is_admin else "",
                    (row["avatar"]    or "") if is_admin else "",
                    public_joined.get(row["username"], ""),
                )
            )

        for row in alias_rows:
            out_conn.execute(
                "INSERT INTO username_aliases (alias, action, canonical, new_name, note, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    row["alias"],
                    row["action"],
                    row["canonical"] or "",
                    row["new_name"] or "",
                    row["note"] or "",
                    row["created_at"] or "",
                )
            )

        out_conn.commit()
        out_conn.close()

        if alias_rows:
            self.stdout.write(f"Skopiowano {len(alias_rows)} rekordów username_aliases.")

        self.stdout.write(self.style.SUCCESS(
            f"Gotowe! {len(merged)} wierszy zapisano do {output_path}"
        ))
