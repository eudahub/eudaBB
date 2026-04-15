"""
Incremental exporter — exports new/changed content to a SQLite file.

Compares current PostgreSQL state against the original import SQLite database
to find:
  - New users (not in source DB)
  - Changed users (password or email changed vs source DB)
  - New topics (archive_topic_id IS NULL), with polls and checklists
  - New posts (archive_post_id IS NULL)
  - Full sections and forums (always exported — small, admin-editable)

Usage:
    python manage.py export_incremental /path/to/source.db /path/to/output_inc.db
    python manage.py export_incremental /path/to/source.db  # auto-names output

Temporary content (is_temporary=True) is excluded by default.
"""

import os
import sqlite3
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from board.models import (
    User, Section, Forum, Topic, Post,
    Poll, PollOption, PollVote,
    Checklist, ChecklistCategory, ChecklistItem, ChecklistUpvote, ChecklistComment,
)


def _dt(val):
    """Format datetime or return empty string."""
    return val.strftime("%Y-%m-%d %H:%M:%S") if val else ""


class Command(BaseCommand):
    help = "Export new/changed content to an incremental SQLite file"

    def add_arguments(self, parser):
        parser.add_argument("source_db", help="Path to original import SQLite DB (e.g. eudaHub.db)")
        parser.add_argument("output_db", nargs="?", default="", help="Output SQLite path (auto-generated if omitted)")
        parser.add_argument("--include-temporary", action="store_true", default=False,
                            help="Include temporary content (default: exclude)")

    def handle(self, *args, **options):
        source_path = options["source_db"]
        if not os.path.isfile(source_path):
            raise CommandError(f"Source DB not found: {source_path}")

        output_path = options["output_db"]
        if not output_path:
            base = os.path.splitext(os.path.basename(source_path))[0]
            date_str = datetime.now().strftime("%Y%m%d")
            output_path = os.path.join(os.path.dirname(source_path), f"{base}_inc{date_str}.db")

        if os.path.exists(output_path):
            raise CommandError(f"Output file already exists: {output_path}")

        include_temp = options["include_temporary"]

        # --- Read source DB users ---
        # If source DB has new_name column, use COALESCE(NULLIF(new_name,''), username)
        # as the key — that's the name imported into PostgreSQL.
        src_conn = sqlite3.connect(source_path)
        src_conn.row_factory = sqlite3.Row
        src_columns = {
            row["name"]
            for row in src_conn.execute("PRAGMA table_info(users)").fetchall()
        }
        has_new_name = "new_name" in src_columns
        if has_new_name:
            name_expr = "COALESCE(NULLIF(new_name,''), username)"
            self.stdout.write("Source DB has new_name column — using it as key.")
        else:
            name_expr = "username"
        src_users = {}
        for row in src_conn.execute(
            f"SELECT {name_expr} AS effective_name, email, pass_hash FROM users"
        ):
            src_users[row["effective_name"]] = {
                "email": (row["email"] or "").strip().lower(),
                "pass_hash": row["pass_hash"] or "",
            }
        src_conn.close()
        self.stdout.write(f"Source DB: {len(src_users)} users")

        # --- Classify PostgreSQL users ---
        new_users = []
        changed_users = []
        pg_users = User.objects.exclude(is_root=True)
        if not include_temp:
            pg_users = pg_users.filter(is_temporary=False)

        for u in pg_users.iterator():
            src = src_users.get(u.username)
            if src is None:
                new_users.append(u)
            else:
                changes = []
                if u.password != src["pass_hash"] and src["pass_hash"]:
                    changes.append("password")
                if u.email != src["email"]:
                    changes.append("email")
                if changes:
                    changed_users.append((u, ",".join(changes)))

        # --- New topics ---
        topics_qs = Topic.objects.filter(archive_topic_id__isnull=True).select_related("forum", "author")
        if not include_temp:
            topics_qs = topics_qs.filter(is_temporary=False)
        new_topics = list(topics_qs)
        new_topic_ids = {t.pk for t in new_topics}

        # --- New posts ---
        posts_qs = Post.objects.filter(archive_post_id__isnull=True).select_related("topic", "author")
        if not include_temp:
            posts_qs = posts_qs.filter(is_temporary=False)
        new_posts = list(posts_qs)

        # --- Polls on new topics ---
        new_polls = list(Poll.objects.filter(topic_id__in=new_topic_ids).select_related("topic"))
        new_poll_ids = {p.pk for p in new_polls}
        new_poll_options = list(PollOption.objects.filter(poll_id__in=new_poll_ids).order_by("poll_id", "sort_order"))
        new_poll_votes = list(PollVote.objects.filter(poll_id__in=new_poll_ids).select_related("user"))

        # --- Checklists on new topics ---
        new_checklists = list(Checklist.objects.filter(topic_id__in=new_topic_ids))
        new_cl_ids = {cl.pk for cl in new_checklists}
        new_cl_categories = list(ChecklistCategory.objects.filter(checklist_id__in=new_cl_ids).order_by("checklist_id", "order"))
        new_cl_items = list(ChecklistItem.objects.filter(checklist_id__in=new_cl_ids).select_related("author", "category", "status_changed_by"))
        new_cl_item_ids = {i.pk for i in new_cl_items}
        new_cl_upvotes = list(ChecklistUpvote.objects.filter(item_id__in=new_cl_item_ids).select_related("user"))
        new_cl_comments = list(ChecklistComment.objects.filter(item_id__in=new_cl_item_ids).select_related("author"))

        # --- Sections & Forums (always full dump) ---
        all_sections = list(Section.objects.all())
        all_forums = list(Forum.objects.select_related("section", "parent").all())

        # === Create output SQLite ===
        out = sqlite3.connect(output_path)
        out.execute("PRAGMA journal_mode=WAL")

        # --- meta ---
        out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        forum_name = getattr(settings, "FORUM", os.environ.get("FORUM", "unknown"))
        out.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("forum", forum_name),
            ("source_db", os.path.basename(source_path)),
            ("export_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("new_users", str(len(new_users))),
            ("changed_users", str(len(changed_users))),
            ("new_topics", str(len(new_topics))),
            ("new_posts", str(len(new_posts))),
            ("polls", str(len(new_polls))),
            ("checklists", str(len(new_checklists))),
        ])

        # --- sections ---
        out.execute("""CREATE TABLE sections (
            section_id INTEGER, title TEXT NOT NULL, "order" INTEGER NOT NULL DEFAULT 0
        )""")
        for s in all_sections:
            out.execute("INSERT INTO sections VALUES (?,?,?)", (s.pk, s.title, s.order))

        # --- forums ---
        out.execute("""CREATE TABLE forums (
            forum_id INTEGER, section_id INTEGER, parent_id INTEGER,
            title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            "order" INTEGER NOT NULL DEFAULT 0,
            access_level INTEGER NOT NULL DEFAULT 0,
            archive_level INTEGER NOT NULL DEFAULT 0
        )""")
        for f in all_forums:
            out.execute("INSERT INTO forums VALUES (?,?,?,?,?,?,?,?)",
                (f.pk, f.section_id, f.parent_id, f.title, f.description,
                 f.order, f.access_level, f.archive_level))

        # --- users ---
        out.execute("""CREATE TABLE users (
            user_id INTEGER, username TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '', password TEXT NOT NULL DEFAULT '',
            signature TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '', role INTEGER NOT NULL DEFAULT 0,
            is_temporary INTEGER NOT NULL DEFAULT 0,
            change_type TEXT NOT NULL DEFAULT 'new', changes TEXT NOT NULL DEFAULT ''
        )""")
        for u in new_users:
            out.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (u.pk, u.username, u.email, u.password, u.signature,
                 u.website, u.location, u.role, int(u.is_temporary), "new", ""))
        for u, changes in changed_users:
            out.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (u.pk, u.username, u.email, u.password, u.signature,
                 u.website, u.location, u.role, int(u.is_temporary), "changed", changes))

        # --- topics ---
        out.execute("""CREATE TABLE topics (
            topic_id INTEGER, forum_id INTEGER, title TEXT NOT NULL,
            author_name TEXT, created_at TEXT,
            is_temporary INTEGER NOT NULL DEFAULT 0,
            is_locked INTEGER NOT NULL DEFAULT 0,
            feature INTEGER NOT NULL DEFAULT 0
        )""")
        for t in new_topics:
            out.execute("INSERT INTO topics VALUES (?,?,?,?,?,?,?,?)",
                (t.pk, t.forum_id, t.title,
                 t.author.username if t.author else "", _dt(t.created_at),
                 int(t.is_temporary), int(t.is_locked), t.feature))

        # --- posts ---
        out.execute("""CREATE TABLE posts (
            post_id INTEGER, topic_id INTEGER, forum_id INTEGER,
            topic_title TEXT, author_name TEXT, title TEXT,
            content TEXT NOT NULL, created_at TEXT,
            post_order INTEGER
        )""")
        for p in new_posts:
            out.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?)",
                (p.pk, p.topic_id, p.topic.forum_id,
                 p.topic.title, p.author.username if p.author else "",
                 p.subject, p.content_bbcode, _dt(p.created_at),
                 p.post_order))

        # --- polls ---
        out.execute("""CREATE TABLE polls (
            poll_id INTEGER, topic_id INTEGER, topic_title TEXT NOT NULL,
            created_at TEXT, ends_at TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            allow_vote_change INTEGER NOT NULL DEFAULT 0,
            allow_multiple_choice INTEGER NOT NULL DEFAULT 0,
            total_votes INTEGER NOT NULL DEFAULT 0
        )""")
        for p in new_polls:
            out.execute("INSERT INTO polls VALUES (?,?,?,?,?,?,?,?,?)",
                (p.pk, p.topic_id, p.topic.title, _dt(p.created_at), _dt(p.ends_at),
                 int(p.is_closed), int(p.allow_vote_change),
                 int(p.allow_multiple_choice), p.total_votes))

        out.execute("""CREATE TABLE poll_options (
            option_id INTEGER, poll_id INTEGER,
            option_text TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
            vote_count INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0
        )""")
        for o in new_poll_options:
            out.execute("INSERT INTO poll_options VALUES (?,?,?,?,?,?)",
                (o.pk, o.poll_id, o.option_text, o.category, o.vote_count, o.sort_order))

        out.execute("""CREATE TABLE poll_votes (
            vote_id INTEGER, poll_id INTEGER, user_username TEXT,
            option_id INTEGER, created_at TEXT
        )""")
        for v in new_poll_votes:
            out.execute("INSERT INTO poll_votes VALUES (?,?,?,?,?)",
                (v.pk, v.poll_id, v.user.username if v.user else "", v.option_id, _dt(v.created_at)))

        # --- checklists ---
        out.execute("""CREATE TABLE checklists (
            checklist_id INTEGER, topic_id INTEGER,
            allow_user_proposals INTEGER NOT NULL DEFAULT 1,
            default_sort TEXT NOT NULL DEFAULT 'upvotes',
            allowed_tags TEXT NOT NULL DEFAULT '',
            is_closed INTEGER NOT NULL DEFAULT 0,
            closed_at TEXT, created_at TEXT
        )""")
        for cl in new_checklists:
            out.execute("INSERT INTO checklists VALUES (?,?,?,?,?,?,?,?)",
                (cl.pk, cl.topic_id, int(cl.allow_user_proposals),
                 cl.default_sort, cl.allowed_tags, int(cl.is_closed),
                 _dt(cl.closed_at), _dt(cl.created_at)))

        out.execute("""CREATE TABLE checklist_categories (
            category_id INTEGER, checklist_id INTEGER,
            name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#6c757d',
            "order" INTEGER NOT NULL DEFAULT 0
        )""")
        for c in new_cl_categories:
            out.execute("INSERT INTO checklist_categories VALUES (?,?,?,?,?)",
                (c.pk, c.checklist_id, c.name, c.color, c.order))

        out.execute("""CREATE TABLE checklist_items (
            item_id INTEGER, checklist_id INTEGER,
            author_username TEXT, author_label TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            category_id INTEGER, tag TEXT NOT NULL DEFAULT '',
            status INTEGER NOT NULL DEFAULT 2,
            priority INTEGER, duplicate_of_id INTEGER,
            rejection_reason TEXT NOT NULL DEFAULT '',
            upvote_count INTEGER NOT NULL DEFAULT 0,
            anon_upvote_count INTEGER NOT NULL DEFAULT 0,
            comment_count INTEGER NOT NULL DEFAULT 0,
            "order" INTEGER NOT NULL DEFAULT 0,
            created_at TEXT, updated_at TEXT,
            status_changed_at TEXT, status_changed_by_username TEXT
        )""")
        for i in new_cl_items:
            out.execute("INSERT INTO checklist_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i.pk, i.checklist_id,
                 i.author.username if i.author else "", i.author_label,
                 i.title, i.description, i.category_id, i.tag, i.status, i.priority,
                 i.duplicate_of_id, i.rejection_reason,
                 i.upvote_count, i.anon_upvote_count, i.comment_count, i.order,
                 _dt(i.created_at), _dt(i.updated_at),
                 _dt(i.status_changed_at),
                 i.status_changed_by.username if i.status_changed_by else ""))

        out.execute("""CREATE TABLE checklist_upvotes (
            upvote_id INTEGER, item_id INTEGER, user_username TEXT, created_at TEXT
        )""")
        for u in new_cl_upvotes:
            out.execute("INSERT INTO checklist_upvotes VALUES (?,?,?,?)",
                (u.pk, u.item_id, u.user.username if u.user else "", _dt(u.created_at)))

        out.execute("""CREATE TABLE checklist_comments (
            comment_id INTEGER, item_id INTEGER,
            author_username TEXT, author_label TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL, created_at TEXT, updated_at TEXT
        )""")
        for c in new_cl_comments:
            out.execute("INSERT INTO checklist_comments VALUES (?,?,?,?,?,?,?)",
                (c.pk, c.item_id,
                 c.author.username if c.author else "", c.author_label,
                 c.content, _dt(c.created_at), _dt(c.updated_at)))

        out.commit()
        out.close()

        self.stdout.write(self.style.SUCCESS(
            f"Eksport: {output_path}\n"
            f"  Sekcje:            {len(all_sections)}\n"
            f"  Fora:              {len(all_forums)}\n"
            f"  Nowi userzy:       {len(new_users)}\n"
            f"  Zmienieni userzy:  {len(changed_users)}\n"
            f"  Nowe topiki:       {len(new_topics)}\n"
            f"  Nowe posty:        {len(new_posts)}\n"
            f"  Ankiety:           {len(new_polls)} ({len(new_poll_options)} opcji, {len(new_poll_votes)} glosow)\n"
            f"  Checklisty:        {len(new_checklists)} ({len(new_cl_items)} pozycji, {len(new_cl_comments)} komentarzy)"
        ))
