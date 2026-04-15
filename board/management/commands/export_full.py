"""
Full exporter — eksportuje całą zawartość forum do SQLite.

Wyklucza:
  - użytkowników tymczasowych (is_temporary=True) i ich treści
  - treści oczekujące (is_pending=True)
  - konto root

Nazwy kolumn zgodne ze standardem archiwum sfinia/phpBB tam gdzie to możliwe:
  - pass_hash zamiast password
  - author_name zamiast author_id/author FK
  - content zamiast content_bbcode
  - www zamiast website
  - parent_forum_id zamiast parent_id

Użycie:
    python manage.py export_full /path/to/output.db
    python manage.py export_full  # auto-generowana nazwa z datą
"""

import os
import re
import sqlite3
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from board.models import (
    User, Section, Forum, Topic, Post,
    Poll, PollOption, PollVote,
    Checklist, ChecklistCategory, ChecklistItem, ChecklistUpvote, ChecklistComment,
    QuoteReference,
)


def _dt(val):
    return val.strftime("%Y-%m-%d %H:%M:%S") if val else ""


def _avatar_path(avatar):
    """Return original filename without Django's random upload suffix.
    'avatars/1692_kGmRlVe.jpg' → '1692.jpg'
    """
    if not avatar:
        return ""
    name = os.path.basename(avatar.name)
    return re.sub(r'_[A-Za-z0-9]+(\.[^.]+)$', r'\1', name)


class Command(BaseCommand):
    help = "Export full forum content to SQLite (archive column naming)"

    def add_arguments(self, parser):
        parser.add_argument("output_db", nargs="?", default="",
                            help="Output SQLite path (auto-generated if omitted)")

    def handle(self, *args, **options):
        output_path = options["output_db"]
        if not output_path:
            forum_name = getattr(settings, "FORUM", os.environ.get("FORUM", "forum"))
            date_str = datetime.now().strftime("%Y%m%d")
            output_path = f"{forum_name}_full_{date_str}.db"

        if os.path.exists(output_path):
            raise CommandError(f"Output file already exists: {output_path}")

        # --- Collect data ---
        temp_user_ids = set(
            User.objects.filter(is_temporary=True).values_list("id", flat=True)
        )

        all_sections = list(Section.objects.order_by("order", "pk"))
        all_forums   = list(Forum.objects.select_related("section", "parent").order_by("section_id", "order", "pk"))

        users = list(
            User.objects.exclude(is_temporary=True).exclude(is_root=True).order_by("pk")
        )

        topics_qs = (
            Topic.objects
            .filter(is_temporary=False, is_pending=False)
            .exclude(author_id__in=temp_user_ids)
            .select_related("author", "forum")
            .prefetch_related("posts")
            .order_by("pk")
        )
        all_topics = list(topics_qs)
        topic_ids  = {t.pk for t in all_topics}

        posts_qs = (
            Post.objects
            .filter(is_temporary=False, is_pending=False, topic_id__in=topic_ids)
            .exclude(author_id__in=temp_user_ids)
            .select_related("author", "topic")
            .order_by("topic_id", "post_order")
        )
        all_posts = list(posts_qs)

        # --- Polls ---
        all_polls        = list(Poll.objects.filter(topic_id__in=topic_ids).select_related("topic"))
        poll_ids         = {p.pk for p in all_polls}
        all_poll_options = list(PollOption.objects.filter(poll_id__in=poll_ids).order_by("poll_id", "sort_order"))
        all_poll_votes   = list(PollVote.objects.filter(poll_id__in=poll_ids).select_related("user", "option"))

        # --- Quotes ---
        post_ids = {p.pk for p in all_posts}
        all_quotes = list(
            QuoteReference.objects
            .filter(post_id__in=post_ids)
            .select_related("post", "source_post")
            .order_by("post_id", "quote_index")
        )

        # --- Checklists ---
        all_checklists = list(Checklist.objects.filter(topic_id__in=topic_ids))
        cl_ids         = {cl.pk for cl in all_checklists}
        all_cl_cats    = list(ChecklistCategory.objects.filter(checklist_id__in=cl_ids).order_by("checklist_id", "order"))
        all_cl_items   = list(ChecklistItem.objects.filter(checklist_id__in=cl_ids)
                              .select_related("author", "category", "status_changed_by").order_by("checklist_id", "order"))
        cl_item_ids    = {i.pk for i in all_cl_items}
        all_cl_upvotes = list(ChecklistUpvote.objects.filter(item_id__in=cl_item_ids).select_related("user"))
        all_cl_comments= list(ChecklistComment.objects.filter(item_id__in=cl_item_ids)
                               .select_related("author").order_by("item_id", "created_at"))

        # === Build SQLite ===
        out = sqlite3.connect(output_path)
        out.execute("PRAGMA journal_mode=WAL")

        # --- meta ---
        out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        forum_name = getattr(settings, "FORUM", os.environ.get("FORUM", "unknown"))
        out.executemany("INSERT INTO meta VALUES (?,?)", [
            ("forum",       forum_name),
            ("export_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("users",       str(len(users))),
            ("topics",      str(len(all_topics))),
            ("posts",       str(len(all_posts))),
            ("polls",       str(len(all_polls))),
            ("checklists",  str(len(all_checklists))),
        ])

        # --- sections (archive schema) ---
        out.execute("""CREATE TABLE sections (
            section_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            "order" INTEGER NOT NULL DEFAULT 0
        )""")
        for s in all_sections:
            out.execute('INSERT INTO sections VALUES (?,?,?)', (s.pk, s.title, s.order))

        # --- forums (archive schema) ---
        out.execute("""CREATE TABLE forums (
            forum_id INTEGER PRIMARY KEY,
            section_id INTEGER,
            parent_forum_id INTEGER,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            "order" INTEGER NOT NULL DEFAULT 0,
            access_level INTEGER NOT NULL DEFAULT 0,
            archive_level INTEGER NOT NULL DEFAULT 0,
            visibility INTEGER NOT NULL DEFAULT 0,
            blog_of TEXT NOT NULL DEFAULT ''
        )""")
        for f in all_forums:
            out.execute('INSERT INTO forums VALUES (?,?,?,?,?,?,?,?,?,?)',
                (f.pk, f.section_id, f.parent_id, f.title, f.description,
                 f.order, f.access_level, f.archive_level,
                 f.access_level, ""))

        # --- users (archive schema: pass_hash, www) ---
        out.execute("""CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            pass_hash TEXT NOT NULL DEFAULT '',
            signature TEXT NOT NULL DEFAULT '',
            website TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            role INTEGER NOT NULL DEFAULT 0,
            avatar_local_path TEXT NOT NULL DEFAULT '',
            joined_at TEXT NOT NULL DEFAULT ''
        )""")
        for u in users:
            out.execute('INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?)',
                (u.pk, u.username, u.email, u.password,
                 u.signature, u.website, u.location, u.role,
                 _avatar_path(u.avatar),
                 _dt(u.date_joined)))

        # --- topics (archive schema) ---
        out.execute("""CREATE TABLE topics (
            topic_id INTEGER PRIMARY KEY,
            forum_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            topic_type TEXT NOT NULL DEFAULT '',
            reply_count INTEGER NOT NULL DEFAULT 0,
            view_count INTEGER NOT NULL DEFAULT 0,
            author_name TEXT,
            last_post_at TEXT,
            last_post_author TEXT,
            has_poll INTEGER NOT NULL DEFAULT 0,
            feature INTEGER NOT NULL DEFAULT 0
        )""")
        topic_poll_ids = {p.topic_id for p in all_polls}
        topic_cl_ids   = {cl.topic_id for cl in all_checklists}
        for t in all_topics:
            non_pending = [p for p in t.posts.all() if not p.is_pending and not p.is_temporary]
            reply_count = max(0, len(non_pending) - 1)
            last_post = max(non_pending, key=lambda p: p.post_order, default=None)
            last_post_at     = _dt(last_post.created_at) if last_post else ""
            last_post_author = last_post.author.username if last_post and last_post.author else ""
            has_poll = 1 if t.pk in topic_poll_ids else (2 if t.pk in topic_cl_ids else 0)
            out.execute('INSERT INTO topics VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (t.pk, t.forum_id, t.title,
                 t.topic_type or "",
                 reply_count, t.view_count or 0,
                 t.author.username if t.author else "",
                 last_post_at, last_post_author,
                 has_poll, t.feature))

        # --- posts (archive schema) ---
        out.execute("""CREATE TABLE posts (
            post_id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL,
            forum_id INTEGER,
            topic_title TEXT,
            author_name TEXT,
            created_at TEXT,
            subject TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            post_order INTEGER NOT NULL DEFAULT 0
        )""")
        for p in all_posts:
            out.execute('INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?)',
                (p.pk, p.topic_id, p.topic.forum_id,
                 p.topic.title,
                 p.author.username if p.author else "",
                 _dt(p.created_at),
                 p.subject,
                 p.content_bbcode,
                 p.post_order))

        # --- quotes (archive schema) ---
        out.execute("""CREATE TABLE quotes (
            id INTEGER PRIMARY KEY,
            post_id INTEGER NOT NULL,
            quoted_user TEXT NOT NULL DEFAULT '',
            quoted_user_resolved TEXT NOT NULL DEFAULT '',
            source_post_id INTEGER,
            quote_text_preview TEXT NOT NULL DEFAULT '',
            quote_index INTEGER NOT NULL DEFAULT 0,
            found INTEGER NOT NULL DEFAULT 0
        )""")
        for q in all_quotes:
            out.execute('INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?)',
                (q.pk, q.post_id,
                 q.quoted_username, q.quoted_username,
                 q.source_post_id,
                 "", q.quote_index,
                 1 if q.source_post_id else 0))

        # --- polls (eudaBB format — no archive equivalent) ---
        out.execute("""CREATE TABLE polls (
            poll_id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL,
            topic_title TEXT NOT NULL DEFAULT '',
            created_at TEXT, ends_at TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            allow_vote_change INTEGER NOT NULL DEFAULT 0,
            allow_multiple_choice INTEGER NOT NULL DEFAULT 0,
            total_votes INTEGER NOT NULL DEFAULT 0
        )""")
        for p in all_polls:
            out.execute('INSERT INTO polls VALUES (?,?,?,?,?,?,?,?,?)',
                (p.pk, p.topic_id, p.topic.title,
                 _dt(p.created_at), _dt(p.ends_at),
                 int(p.is_closed), int(p.allow_vote_change),
                 int(p.allow_multiple_choice), p.total_votes))

        out.execute("""CREATE TABLE poll_options (
            option_id INTEGER PRIMARY KEY,
            poll_id INTEGER NOT NULL,
            option_text TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            vote_count INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0
        )""")
        for o in all_poll_options:
            out.execute('INSERT INTO poll_options VALUES (?,?,?,?,?,?)',
                (o.pk, o.poll_id, o.option_text, o.category, o.vote_count, o.sort_order))

        out.execute("""CREATE TABLE poll_votes (
            vote_id INTEGER PRIMARY KEY,
            poll_id INTEGER NOT NULL,
            user_username TEXT NOT NULL,
            option_id INTEGER NOT NULL,
            created_at TEXT
        )""")
        for v in all_poll_votes:
            out.execute('INSERT INTO poll_votes VALUES (?,?,?,?,?)',
                (v.pk, v.poll_id,
                 v.user.username if v.user else "",
                 v.option_id, _dt(v.created_at)))

        # --- checklists (eudaBB format) ---
        out.execute("""CREATE TABLE checklists (
            checklist_id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL,
            allow_user_proposals INTEGER NOT NULL DEFAULT 1,
            default_sort TEXT NOT NULL DEFAULT 'upvotes',
            allowed_tags TEXT NOT NULL DEFAULT '',
            is_closed INTEGER NOT NULL DEFAULT 0,
            closed_at TEXT, created_at TEXT
        )""")
        for cl in all_checklists:
            out.execute('INSERT INTO checklists VALUES (?,?,?,?,?,?,?,?)',
                (cl.pk, cl.topic_id, int(cl.allow_user_proposals),
                 cl.default_sort, cl.allowed_tags, int(cl.is_closed),
                 _dt(cl.closed_at), _dt(cl.created_at)))

        out.execute("""CREATE TABLE checklist_categories (
            category_id INTEGER PRIMARY KEY,
            checklist_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6c757d',
            "order" INTEGER NOT NULL DEFAULT 0
        )""")
        for c in all_cl_cats:
            out.execute('INSERT INTO checklist_categories VALUES (?,?,?,?,?)',
                (c.pk, c.checklist_id, c.name, c.color, c.order))

        out.execute("""CREATE TABLE checklist_items (
            item_id INTEGER PRIMARY KEY,
            checklist_id INTEGER NOT NULL,
            author_name TEXT, author_label TEXT NOT NULL DEFAULT '',
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
            status_changed_at TEXT, status_changed_by_name TEXT
        )""")
        for i in all_cl_items:
            out.execute('INSERT INTO checklist_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (i.pk, i.checklist_id,
                 i.author.username if i.author else "", i.author_label,
                 i.title, i.description, i.category_id, i.tag,
                 i.status, i.priority, i.duplicate_of_id, i.rejection_reason,
                 i.upvote_count, i.anon_upvote_count, i.comment_count, i.order,
                 _dt(i.created_at), _dt(i.updated_at),
                 _dt(i.status_changed_at),
                 i.status_changed_by.username if i.status_changed_by else ""))

        out.execute("""CREATE TABLE checklist_upvotes (
            upvote_id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            user_username TEXT NOT NULL,
            created_at TEXT
        )""")
        for u in all_cl_upvotes:
            out.execute('INSERT INTO checklist_upvotes VALUES (?,?,?,?)',
                (u.pk, u.item_id,
                 u.user.username if u.user else "", _dt(u.created_at)))

        out.execute("""CREATE TABLE checklist_comments (
            comment_id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            author_name TEXT, author_label TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            created_at TEXT, updated_at TEXT
        )""")
        for c in all_cl_comments:
            out.execute('INSERT INTO checklist_comments VALUES (?,?,?,?,?,?,?)',
                (c.pk, c.item_id,
                 c.author.username if c.author else "", c.author_label,
                 c.content, _dt(c.created_at), _dt(c.updated_at)))

        out.commit()
        out.close()

        self.stdout.write(self.style.SUCCESS(
            f"Eksport: {output_path}\n"
            f"  Sekcje:       {len(all_sections)}\n"
            f"  Fora:         {len(all_forums)}\n"
            f"  Użytkownicy:  {len(users)}\n"
            f"  Topiki:       {len(all_topics)}\n"
            f"  Posty:        {len(all_posts)}\n"
            f"  Cytaty:       {len(all_quotes)}\n"
            f"  Ankiety:      {len(all_polls)} ({len(all_poll_options)} opcji, {len(all_poll_votes)} głosów)\n"
            f"  Checklisty:   {len(all_checklists)} ({len(all_cl_items)} pozycji)"
        ))
