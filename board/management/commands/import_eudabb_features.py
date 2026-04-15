"""
Import polls and checklists from an eudaBB-format SQLite export.

Reads tables: polls, poll_options, poll_votes,
              checklists, checklist_categories, checklist_items,
              checklist_upvotes, checklist_comments

Maps archive topic_id → Django Topic via Topic.archive_topic_id.
Maps archive user_username → Django User.

Usage:
    python manage.py import_eudabb_features /path/to/eudaHub.db
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from board.models import (
    Topic, User,
    Poll, PollOption, PollVote,
    Checklist, ChecklistCategory, ChecklistItem, ChecklistUpvote, ChecklistComment,
)

_UTC = ZoneInfo("UTC")


def _dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Import polls and checklists from eudaBB-format SQLite export"

    def add_arguments(self, parser):
        parser.add_argument("archive_db", help="Path to eudaBB SQLite export (e.g. eudaHub.db)")
        parser.add_argument("--clear", action="store_true",
                            help="Delete existing polls and checklists before import")

    def handle(self, *args, **options):
        db_path = options["archive_db"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        def has_table(name):
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None

        if options["clear"]:
            Poll.objects.all().delete()
            Checklist.objects.all().delete()
            self.stdout.write("Cleared existing polls and checklists.")

        # --- Lookup maps ---
        topic_map = {t.archive_topic_id: t for t in Topic.objects.exclude(archive_topic_id=None)}
        user_map = {u.username: u for u in User.objects.all()}

        polls_imported = cl_imported = 0

        with transaction.atomic():
            # ── Polls ────────────────────────────────────────────────────────
            if has_table("polls"):
                poll_rows = conn.execute("SELECT * FROM polls ORDER BY poll_id").fetchall()
                option_rows = conn.execute("SELECT * FROM poll_options ORDER BY poll_id, sort_order").fetchall()
                vote_rows = conn.execute("SELECT * FROM poll_votes").fetchall() if has_table("poll_votes") else []

                # Group options and votes by poll_id
                from collections import defaultdict
                options_by_poll = defaultdict(list)
                for o in option_rows:
                    options_by_poll[o["poll_id"]].append(o)
                votes_by_poll = defaultdict(list)
                for v in vote_rows:
                    votes_by_poll[v["poll_id"]].append(v)

                for row in poll_rows:
                    topic = topic_map.get(row["topic_id"])
                    if topic is None:
                        self.stderr.write(f"Poll: brak topiku archive_topic_id={row['topic_id']}, pomijam.")
                        continue

                    keys = row.keys()
                    question_text = (row["topic_title"] if "topic_title" in keys else None) or ""
                    poll, _ = Poll.objects.update_or_create(
                        topic=topic,
                        defaults={
                            "question": question_text,
                            "created_at": _dt(row["created_at"]) or topic.created_at,
                            "ends_at": _dt(row["ends_at"]),
                            "is_closed": bool(row["is_closed"]),
                            "allow_vote_change": bool(row["allow_vote_change"]),
                            "allow_multiple_choice": bool(row["allow_multiple_choice"]),
                            "total_votes": row["total_votes"] or 0,
                            "is_archived_import": False,
                        },
                    )
                    poll.options.all().delete()

                    # archive option_id → new PollOption (for vote mapping)
                    option_id_map = {}
                    PollOption.objects.bulk_create([
                        PollOption(
                            poll=poll,
                            option_text=o["option_text"],
                            category=o["category"] or "",
                            vote_count=o["vote_count"] or 0,
                            sort_order=o["sort_order"] or i,
                        )
                        for i, o in enumerate(options_by_poll[row["poll_id"]], start=1)
                    ])
                    # rebuild option_id_map after bulk_create
                    for opt_obj, opt_row in zip(
                        poll.options.order_by("sort_order"),
                        options_by_poll[row["poll_id"]],
                    ):
                        option_id_map[opt_row["option_id"]] = opt_obj

                    # Votes
                    vote_objs = []
                    for v in votes_by_poll[row["poll_id"]]:
                        user = user_map.get(v["user_username"])
                        option = option_id_map.get(v["option_id"])
                        if user and option:
                            vote_objs.append(PollVote(poll=poll, user=user, option=option))
                    if vote_objs:
                        PollVote.objects.bulk_create(vote_objs, ignore_conflicts=True)

                    polls_imported += 1

                self.stdout.write(f"  Ankiety: {polls_imported} zaimportowane")

            # ── Checklists ───────────────────────────────────────────────────
            if has_table("checklists"):
                cl_rows = conn.execute("SELECT * FROM checklists ORDER BY checklist_id").fetchall()
                cat_rows = conn.execute("SELECT * FROM checklist_categories ORDER BY checklist_id, \"order\"").fetchall() if has_table("checklist_categories") else []

                item_rows = conn.execute("SELECT * FROM checklist_items ORDER BY checklist_id, \"order\"").fetchall() if has_table("checklist_items") else []
                upvote_rows = conn.execute("SELECT * FROM checklist_upvotes").fetchall() if has_table("checklist_upvotes") else []
                comment_rows = conn.execute("SELECT * FROM checklist_comments ORDER BY item_id, created_at").fetchall() if has_table("checklist_comments") else []

                from collections import defaultdict
                cats_by_cl = defaultdict(list)
                for c in cat_rows:
                    cats_by_cl[c["checklist_id"]].append(c)
                items_by_cl = defaultdict(list)
                for i in item_rows:
                    items_by_cl[i["checklist_id"]].append(i)
                upvotes_by_item = defaultdict(list)
                for u in upvote_rows:
                    upvotes_by_item[u["item_id"]].append(u)
                comments_by_item = defaultdict(list)
                for c in comment_rows:
                    comments_by_item[c["item_id"]].append(c)

                for row in cl_rows:
                    topic = topic_map.get(row["topic_id"])
                    if topic is None:
                        self.stderr.write(f"Checklist: brak topiku archive_topic_id={row['topic_id']}, pomijam.")
                        continue

                    cl, _ = Checklist.objects.update_or_create(
                        topic=topic,
                        defaults={
                            "allow_user_proposals": bool(row["allow_user_proposals"]),
                            "default_sort": row["default_sort"] or "upvotes",
                            "allowed_tags": row["allowed_tags"] or "",
                            "is_closed": bool(row["is_closed"]),
                            "closed_at": _dt(row["closed_at"]),
                            "created_at": _dt(row["created_at"]) or topic.created_at,
                        },
                    )
                    cl.categories.all().delete()
                    cl.items.all().delete()

                    # Categories: archive cat_id → new ChecklistCategory
                    cat_id_map = {}
                    for cat_row in cats_by_cl[row["checklist_id"]]:
                        cat_obj = ChecklistCategory.objects.create(
                            checklist=cl,
                            name=cat_row["name"],
                            color=cat_row["color"] or "#6c757d",
                            order=cat_row["order"] or 0,
                        )
                        cat_id_map[cat_row["category_id"]] = cat_obj

                    # Items: archive item_id → new ChecklistItem
                    item_id_map = {}
                    for item_row in items_by_cl[row["checklist_id"]]:
                        author = user_map.get(item_row["author_name"]) if item_row["author_name"] else None
                        status_changed_by = user_map.get(item_row["status_changed_by_name"]) if item_row["status_changed_by_name"] else None
                        cat = cat_id_map.get(item_row["category_id"]) if item_row["category_id"] else None
                        item_obj = ChecklistItem(
                            checklist=cl,
                            author=author,
                            author_label=item_row["author_label"] or "",
                            title=item_row["title"],
                            description=item_row["description"] or "",
                            category=cat,
                            tag=item_row["tag"] or "",
                            status=item_row["status"] if item_row["status"] is not None else 2,
                            priority=item_row["priority"],
                            rejection_reason=item_row["rejection_reason"] or "",
                            upvote_count=item_row["upvote_count"] or 0,
                            anon_upvote_count=item_row["anon_upvote_count"] or 0,
                            comment_count=item_row["comment_count"] or 0,
                            order=item_row["order"] or 0,
                            status_changed_by=status_changed_by,
                        )
                        if item_row["created_at"]:
                            item_obj.created_at = _dt(item_row["created_at"])
                        if item_row["status_changed_at"]:
                            item_obj.status_changed_at = _dt(item_row["status_changed_at"])
                        item_obj.save()
                        item_id_map[item_row["item_id"]] = item_obj

                    # duplicate_of (self-FK) — second pass
                    for item_row in items_by_cl[row["checklist_id"]]:
                        if item_row["duplicate_of_id"]:
                            dup = item_id_map.get(item_row["duplicate_of_id"])
                            obj = item_id_map.get(item_row["item_id"])
                            if dup and obj:
                                obj.duplicate_of = dup
                                obj.save(update_fields=["duplicate_of"])

                    # Upvotes
                    for item_row in items_by_cl[row["checklist_id"]]:
                        item_obj = item_id_map.get(item_row["item_id"])
                        if not item_obj:
                            continue
                        upvote_objs = []
                        for uv in upvotes_by_item[item_row["item_id"]]:
                            user = user_map.get(uv["user_username"])
                            if user:
                                upvote_objs.append(ChecklistUpvote(item=item_obj, user=user))
                        if upvote_objs:
                            ChecklistUpvote.objects.bulk_create(upvote_objs, ignore_conflicts=True)

                    # Comments
                    for item_row in items_by_cl[row["checklist_id"]]:
                        item_obj = item_id_map.get(item_row["item_id"])
                        if not item_obj:
                            continue
                        for com in comments_by_item[item_row["item_id"]]:
                            author = user_map.get(com["author_name"]) if com["author_name"] else None
                            ChecklistComment.objects.create(
                                item=item_obj,
                                author=author,
                                author_label=com["author_label"] or "",
                                content=com["content"],
                                created_at=_dt(com["created_at"]) or topic.created_at,
                            )

                    cl_imported += 1

                self.stdout.write(f"  Checklisty: {cl_imported} zaimportowane")

        conn.close()
        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Ankiety: {polls_imported}, Checklisty: {cl_imported}"
        ))
