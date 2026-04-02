"""
Build a sanitized import database: no plaintext emails, pre-computed Argon2 hashes.

Usage:
    python manage.py build_import_db \\
        /path/to/sfinia_users_admin.db \\
        /path/to/sfinia_users_real.db \\
        /path/to/output.db

Sources:
  sfinia_users_admin.db — 1036 logged-in users with emails, signatures, etc.
  sfinia_users_real.db  — 3755 all users (logged + guests), spam stats only

Output DB schema (users table):
  user_id, username, has_email, email_hash, email_mask,
  signature, website, location, avatar,
  argon2_memory, argon2_time, argon2_parallel
"""

import sqlite3
import sys
import time

from django.core.management.base import BaseCommand, CommandError

from board.email_utils import mask_email

ARGON2_MEMORY   = 262144
ARGON2_TIME     = 2
ARGON2_PARALLEL = 1
ARGON2_HASHLEN  = 32
ARGON2_SALTLEN  = 16


def _hash_email(email: str) -> str:
    """Return hex-encoded argon2id hash of normalised email (deterministic salt).

    Uses board.email_utils.hash_email — same parameters and salt as the web app,
    enabling O(1) DB lookup by email hash.
    """
    from board.email_utils import hash_email
    return hash_email(email)


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER,
    username     TEXT    NOT NULL,
    has_email    INTEGER NOT NULL DEFAULT 0,
    email_hash   TEXT    NOT NULL DEFAULT '',
    email_mask   TEXT    NOT NULL DEFAULT '',
    signature    TEXT    NOT NULL DEFAULT '',
    website      TEXT    NOT NULL DEFAULT '',
    location     TEXT    NOT NULL DEFAULT '',
    avatar       TEXT    NOT NULL DEFAULT '',
    argon2_memory   INTEGER NOT NULL DEFAULT 262144,
    argon2_time     INTEGER NOT NULL DEFAULT 2,
    argon2_parallel INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_username ON users(username);
"""


class Command(BaseCommand):
    help = "Build sanitized import DB (no plaintext emails, pre-hashed)"

    def add_arguments(self, parser):
        parser.add_argument("admin_db",  help="Path to sfinia_users_admin.db")
        parser.add_argument("real_db",   help="Path to sfinia_users_real.db")
        parser.add_argument("output_db", help="Path to output .db file")

    def handle(self, *args, **options):
        admin_path  = options["admin_db"]
        real_path   = options["real_db"]
        output_path = options["output_db"]

        # Load admin users (have email + profile)
        admin_conn = sqlite3.connect(admin_path)
        admin_conn.row_factory = sqlite3.Row
        admin_rows = {
            r["username"]: r
            for r in admin_conn.execute(
                "SELECT user_id, username, email, signature, website, location, avatar "
                "FROM admin_users ORDER BY user_id"
            ).fetchall()
        }
        admin_conn.close()

        # Load real users (full list, no emails)
        real_conn = sqlite3.connect(real_path)
        real_conn.row_factory = sqlite3.Row
        real_rows = real_conn.execute(
            "SELECT user_id, username FROM users ORDER BY user_id"
        ).fetchall()
        real_conn.close()

        # Build merged list: admin users first (have emails), then real-only users
        seen = set()
        merged = []
        for r in admin_rows.values():
            merged.append(("admin", r))
            seen.add(r["username"])
        for r in real_rows:
            if r["username"] not in seen:
                merged.append(("real", r))
                seen.add(r["username"])

        with_email    = [r for t, r in merged if t == "admin" and r["email"]]
        without_email = [r for t, r in merged if t != "admin" or not r["email"]]

        self.stdout.write(
            f"Łącznie: {len(merged)} userów  "
            f"(z emailem: {len(with_email)}, bez: {len(without_email)})"
        )
        self.stdout.write(
            f"Liczenie {len(with_email)} hashów Argon2id "
            f"m={ARGON2_MEMORY} t={ARGON2_TIME} p={ARGON2_PARALLEL}…"
        )
        self.stdout.write("(szacowany czas: ~%.0f s)" % (len(with_email) * 0.35))

        out_conn = sqlite3.connect(output_path)
        out_conn.executescript(CREATE_SQL)

        done = 0
        t_start = time.time()

        for tag, row in merged:
            username  = row["username"]
            is_admin  = (tag == "admin")
            raw_email = row["email"].strip() if is_admin and row["email"] else ""

            if raw_email:
                email_hash = _hash_email(raw_email)
                email_mask = mask_email(raw_email)
                has_email  = 1
            else:
                email_hash = ""
                email_mask = ""
                has_email  = 0

            out_conn.execute(
                "INSERT INTO users "
                "(user_id, username, has_email, email_hash, email_mask, "
                " signature, website, location, avatar, "
                " argon2_memory, argon2_time, argon2_parallel) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["user_id"] if "user_id" in row.keys() else None,
                    username,
                    has_email,
                    email_hash,
                    email_mask,
                    (row["signature"] or "") if is_admin else "",
                    (row["website"]   or "") if is_admin else "",
                    (row["location"]  or "") if is_admin else "",
                    (row["avatar"]    or "") if is_admin else "",
                    ARGON2_MEMORY,
                    ARGON2_TIME,
                    ARGON2_PARALLEL,
                )
            )

            if raw_email:
                done += 1
                if done % 50 == 0:
                    elapsed = time.time() - t_start
                    remaining = (len(with_email) - done) * (elapsed / done)
                    self.stdout.write(
                        f"  {done}/{len(with_email)}  "
                        f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)"
                    )
                    sys.stdout.flush()

        out_conn.commit()
        out_conn.close()

        elapsed = time.time() - t_start
        self.stdout.write(self.style.SUCCESS(
            f"\nGotowe! {len(merged)} wierszy zapisano do {output_path} "
            f"({elapsed:.0f}s)"
        ))
