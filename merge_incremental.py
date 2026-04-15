#!/usr/bin/env python3
"""
merge_incremental.py — merge an incremental eudaBB export into a base export.

Usage:
    python merge_incremental.py base.db incremental.db output.db

Rules:
  - sections / forums     : UPSERT — columns present in incremental are updated/inserted,
                            extra columns in base are preserved for existing rows
  - users change_type=changed : UPDATE by username (password, email, role, etc.)
  - users change_type=new     : INSERT with new_id = max(base_user_id) + inc_user_id
  - topics / posts / polls / checklists (and sub-tables)
                          : INSERT with new_id = max(base_id) + inc_id
                            FK references within incremental are remapped accordingly

The output DB starts as a copy of the base, then the incremental is applied on top.
Two topics named "Regulamin" (one from base, one from incremental) will both appear —
clean up manually after import.
"""

import sys
import shutil
import sqlite3


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def max_id(cur, table, id_col):
    row = cur.execute(f"SELECT MAX({id_col}) FROM {table}").fetchone()
    return row[0] or 0


def has_table(cur, name):
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def get_columns(cur, table):
    """Return list of column names for a table."""
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]


def insert_row(out, table, col_names, values):
    """INSERT a single row using only named columns (safe with reserved keywords)."""
    col_str = ", ".join(f'"{c}"' for c in col_names)
    ph = ", ".join("?" for _ in col_names)
    out.execute(f"INSERT INTO {table} ({col_str}) VALUES ({ph})", values)


def inc_rows(inc, out, table):
    """Yield (col_names, row_dict) for every row in inc table.

    col_names = columns present in inc AND out, PLUS any out columns that are
    NOT NULL with no DEFAULT (filled with '' for TEXT, 0 for numeric).
    This prevents IntegrityError when the base table has NOT NULL columns
    absent from the incremental export.
    The row_dict contains all inc columns (for FK remapping) plus synthetic defaults.
    """
    inc_cols = get_columns(inc.cursor(), table)
    inc_col_set = set(inc_cols)

    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
    out_info = out.cursor().execute(f"PRAGMA table_info({table})").fetchall()
    out_col_set = {r[1] for r in out_info}
    common = [c for c in inc_cols if c in out_col_set]

    # Columns in out that are NOT NULL, have no DEFAULT, and are absent from inc
    # — we must supply a value or INSERT will fail
    extra = []
    for r in out_info:
        name, notnull, dflt = r[1], r[3], r[4]
        if name not in inc_col_set and notnull and dflt is None:
            col_type = r[2].upper()
            fallback = 0 if any(t in col_type for t in ("INT", "REAL", "NUM", "FLOAT")) else ""
            extra.append((name, fallback))

    all_cols = common + [name for name, _ in extra]

    sel = ", ".join(f'"{c}"' for c in inc_cols)
    for r in inc.execute(f"SELECT {sel} FROM {table}"):
        row = dict(zip(inc_cols, tuple(r)))
        for name, fallback in extra:
            row[name] = fallback
        yield all_cols, row


def upsert_table(out, inc, table, pk_col):
    """Merge inc rows into out table using only columns present in inc.

    - Existing rows (by pk): UPDATE only the inc columns, preserving extra base columns.
    - New rows (pk not in out): INSERT with inc columns, base defaults fill the rest.

    Returns (inserted, updated).
    """
    inc_cols = get_columns(inc.cursor(), table)
    out_cols = set(get_columns(out.cursor(), table))
    common = [c for c in inc_cols if c in out_cols]

    existing_ids = {r[0] for r in out.execute(f'SELECT "{pk_col}" FROM {table}')}

    inserted = updated = 0
    sel = ", ".join(f'"{c}"' for c in inc_cols)
    for r in inc.execute(f"SELECT {sel} FROM {table}"):
        row = dict(zip(inc_cols, tuple(r)))
        pk_val = row[pk_col]

        if pk_val in existing_ids:
            upd_cols = [c for c in common if c != pk_col]
            if upd_cols:
                set_clause = ", ".join(f'"{c}"=?' for c in upd_cols)
                vals = [row[c] for c in upd_cols] + [pk_val]
                out.execute(f'UPDATE {table} SET {set_clause} WHERE "{pk_col}"=?', vals)
            updated += 1
        else:
            col_str = ", ".join(f'"{c}"' for c in common)
            ph = ", ".join("?" for _ in common)
            vals = [row[c] for c in common]
            out.execute(f"INSERT INTO {table} ({col_str}) VALUES ({ph})", vals)
            inserted += 1

    return inserted, updated


def merge(base_path, inc_path, out_path):
    # ── 1. Copy base → output ────────────────────────────────────────────────
    shutil.copy2(base_path, out_path)
    print(f"Copied {base_path} → {out_path}")

    out = sqlite3.connect(out_path)
    out.row_factory = sqlite3.Row
    inc = sqlite3.connect(inc_path)
    inc.row_factory = sqlite3.Row

    # ── 2. Sections & Forums — upsert (preserve extra base columns) ──────────
    ins, upd = upsert_table(out, inc, "sections", "section_id")
    print(f"  sections: {ins} inserted, {upd} updated")

    ins, upd = upsert_table(out, inc, "forums", "forum_id")
    print(f"  forums:   {ins} inserted, {upd} updated")

    # ── 3. Users ─────────────────────────────────────────────────────────────
    user_offset = max_id(out.cursor(), "users", "user_id")
    # build username→user_id map for base
    base_username_to_id = {
        r["username"]: r["user_id"]
        for r in out.execute("SELECT user_id, username FROM users")
    }

    updated_users = 0
    inserted_users = 0
    inc_user_id_map = {}   # inc_user_id → out_user_id (for new users)

    # Columns that exist in both inc users table and out users table
    inc_user_cols = get_columns(inc.cursor(), "users")
    out_user_cols = set(get_columns(out.cursor(), "users"))
    # updatable columns (present in inc, not PK, not change_type/changes metadata)
    upd_cols = [c for c in inc_user_cols if c in out_user_cols
                and c not in ("user_id", "username", "change_type", "changes")]
    # insertable columns (present in both, except change_type/changes which we set ourselves)
    ins_cols_base = [c for c in inc_user_cols if c in out_user_cols
                     and c not in ("change_type", "changes")]

    for cols, row in inc_rows(inc, out, "users"):
        change_type = row["change_type"]
        if change_type == "changed":
            # Update existing user by username
            existing_id = base_username_to_id.get(row["username"])
            if existing_id is not None:
                set_clause = ", ".join(f'"{c}"=?' for c in upd_cols)
                out.execute(
                    f'UPDATE users SET {set_clause} WHERE "user_id"=?',
                    [row[c] for c in upd_cols] + [existing_id],
                )
                updated_users += 1
            else:
                # Username not found in base — insert as new
                new_id = user_offset + row["user_id"]
                inc_user_id_map[row["user_id"]] = new_id
                row["user_id"] = new_id
                row["change_type"] = "new"
                row["changes"] = ""
                all_ins_cols = ins_cols_base + [c for c in ("change_type", "changes") if c in out_user_cols]
                insert_row(out, "users", all_ins_cols, [row[c] for c in all_ins_cols])
                inserted_users += 1
        else:
            # New user
            new_id = user_offset + row["user_id"]
            inc_user_id_map[row["user_id"]] = new_id
            row["user_id"] = new_id
            row["change_type"] = "new"
            row["changes"] = ""
            all_ins_cols = ins_cols_base + [c for c in ("change_type", "changes") if c in out_user_cols]
            insert_row(out, "users", all_ins_cols, [row[c] for c in all_ins_cols])
            inserted_users += 1

    print(f"  users:    {updated_users} updated, {inserted_users} inserted")

    # ── 4. Topics ─────────────────────────────────────────────────────────────
    topic_offset = max_id(out.cursor(), "topics", "topic_id")
    topic_id_map = {}  # inc_topic_id → out_topic_id

    for cols, row in inc_rows(inc, out, "topics"):
        new_id = topic_offset + row["topic_id"]
        topic_id_map[row["topic_id"]] = new_id
        row["topic_id"] = new_id
        insert_row(out, "topics", cols, [row[c] for c in cols])

    print(f"  topics:   {len(topic_id_map)} inserted (offset +{topic_offset})")

    # ── 5. Posts ──────────────────────────────────────────────────────────────
    post_offset = max_id(out.cursor(), "posts", "post_id")
    post_id_map = {}

    for cols, row in inc_rows(inc, out, "posts"):
        new_id = post_offset + row["post_id"]
        post_id_map[row["post_id"]] = new_id
        row["post_id"] = new_id
        row["topic_id"] = topic_id_map.get(row["topic_id"], row["topic_id"])
        insert_row(out, "posts", cols, [row[c] for c in cols])

    print(f"  posts:    {len(post_id_map)} inserted (offset +{post_offset})")

    # ── 6. Polls ──────────────────────────────────────────────────────────────
    if has_table(inc.cursor(), "polls") and has_table(out.cursor(), "polls"):
        poll_offset = max_id(out.cursor(), "polls", "poll_id")
        poll_id_map = {}

        for cols, row in inc_rows(inc, out, "polls"):
            new_id = poll_offset + row["poll_id"]
            poll_id_map[row["poll_id"]] = new_id
            row["poll_id"] = new_id
            row["topic_id"] = topic_id_map.get(row["topic_id"], row["topic_id"])
            insert_row(out, "polls", cols, [row[c] for c in cols])

        print(f"  polls:    {len(poll_id_map)} inserted")

        # Poll options
        option_offset = max_id(out.cursor(), "poll_options", "option_id")
        option_id_map = {}
        for cols, row in inc_rows(inc, out, "poll_options"):
            new_id = option_offset + row["option_id"]
            option_id_map[row["option_id"]] = new_id
            row["option_id"] = new_id
            row["poll_id"] = poll_id_map.get(row["poll_id"], row["poll_id"])
            insert_row(out, "poll_options", cols, [row[c] for c in cols])

        # Poll votes
        vote_offset = max_id(out.cursor(), "poll_votes", "vote_id")
        for cols, row in inc_rows(inc, out, "poll_votes"):
            row["vote_id"] = vote_offset + row["vote_id"]
            row["poll_id"] = poll_id_map.get(row["poll_id"], row["poll_id"])
            row["option_id"] = option_id_map.get(row["option_id"], row["option_id"])
            insert_row(out, "poll_votes", cols, [row[c] for c in cols])

    # ── 7. Checklists ─────────────────────────────────────────────────────────
    if has_table(inc.cursor(), "checklists") and has_table(out.cursor(), "checklists"):
        cl_offset = max_id(out.cursor(), "checklists", "checklist_id")
        cl_id_map = {}

        for cols, row in inc_rows(inc, out, "checklists"):
            new_id = cl_offset + row["checklist_id"]
            cl_id_map[row["checklist_id"]] = new_id
            row["checklist_id"] = new_id
            row["topic_id"] = topic_id_map.get(row["topic_id"], row["topic_id"])
            insert_row(out, "checklists", cols, [row[c] for c in cols])

        print(f"  checklists: {len(cl_id_map)} inserted")

        # Checklist categories
        cat_offset = max_id(out.cursor(), "checklist_categories", "category_id")
        cat_id_map = {}
        for cols, row in inc_rows(inc, out, "checklist_categories"):
            new_id = cat_offset + row["category_id"]
            cat_id_map[row["category_id"]] = new_id
            row["category_id"] = new_id
            row["checklist_id"] = cl_id_map.get(row["checklist_id"], row["checklist_id"])
            insert_row(out, "checklist_categories", cols, [row[c] for c in cols])

        # Checklist items
        item_offset = max_id(out.cursor(), "checklist_items", "item_id")
        item_id_map = {}
        for cols, row in inc_rows(inc, out, "checklist_items"):
            new_id = item_offset + row["item_id"]
            item_id_map[row["item_id"]] = new_id
            row["item_id"] = new_id
            row["checklist_id"] = cl_id_map.get(row["checklist_id"], row["checklist_id"])
            if row.get("category_id"):
                row["category_id"] = cat_id_map.get(row["category_id"], row["category_id"])
            if row.get("duplicate_of_id"):
                row["duplicate_of_id"] = item_id_map.get(row["duplicate_of_id"], row["duplicate_of_id"])
            insert_row(out, "checklist_items", cols, [row[c] for c in cols])

        # Checklist upvotes
        upvote_offset = max_id(out.cursor(), "checklist_upvotes", "upvote_id")
        for cols, row in inc_rows(inc, out, "checklist_upvotes"):
            row["upvote_id"] = upvote_offset + row["upvote_id"]
            row["item_id"] = item_id_map.get(row["item_id"], row["item_id"])
            insert_row(out, "checklist_upvotes", cols, [row[c] for c in cols])

        # Checklist comments
        comment_offset = max_id(out.cursor(), "checklist_comments", "comment_id")
        for cols, row in inc_rows(inc, out, "checklist_comments"):
            row["comment_id"] = comment_offset + row["comment_id"]
            row["item_id"] = item_id_map.get(row["item_id"], row["item_id"])
            insert_row(out, "checklist_comments", cols, [row[c] for c in cols])

    # ── 8. Update meta ────────────────────────────────────────────────────────
    if has_table(out.cursor(), "meta"):
        from datetime import datetime
        out.execute("INSERT OR REPLACE INTO meta VALUES ('merged_from_inc', ?)",
                    (inc.execute("SELECT value FROM meta WHERE key='export_date'").fetchone()[0]
                     if has_table(inc.cursor(), "meta") else "",))
        out.execute("INSERT OR REPLACE INTO meta VALUES ('merge_date', ?)",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))

    # ── 9. Copy tables present in inc but absent from out ────────────────────
    inc_tables = {
        r[0] for r in inc.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    out_tables = {
        r[0] for r in out.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = inc_tables - out_tables
    for tbl in sorted(missing):
        # Reproduce CREATE TABLE statement from inc
        ddl = inc.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()[0]
        out.execute(ddl)
        cols = get_columns(inc.cursor(), tbl)
        sel = ", ".join(f'"{c}"' for c in cols)
        col_str = ", ".join(f'"{c}"' for c in cols)
        ph = ", ".join("?" for _ in cols)
        rows = inc.execute(f"SELECT {sel} FROM {tbl}").fetchall()
        out.executemany(f"INSERT INTO {tbl} ({col_str}) VALUES ({ph})", rows)
        print(f"  {tbl}: copied {len(rows)} rows (new table)")

    out.commit()
    out.close()
    inc.close()
    print(f"\nDone → {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: merge_incremental.py base.db incremental.db output.db")
        sys.exit(1)
    base_path, inc_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    if out_path == base_path or out_path == inc_path:
        die("Output path must differ from both inputs.")
    import os
    if os.path.exists(out_path):
        die(f"Output already exists: {out_path}  — remove it first.")
    merge(base_path, inc_path, out_path)
