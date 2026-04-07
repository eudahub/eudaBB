"""
Import posts (and their topics) from sfiniabb.db.

Sampling modes (pick one):
  --first N      first N posts by post_id
  --last N       last N posts by post_id
  --every N      every N-th post (e.g. --every 40 gives ~11k posts)
  --random N     N random posts
  (no flag)      all posts — WARNING: 438k posts, takes a long time

Post order within each imported topic is re-numbered 1,2,3…
(equivalent to ROW_NUMBER() OVER (PARTITION BY topic_id ORDER BY post_order))
so gaps from sampling are removed.

Usage examples:
  python manage.py import_posts sfiniabb.db --first 1000
  python manage.py import_posts sfiniabb.db --every 40
  python manage.py import_posts sfiniabb.db --random 10000
"""

import re
import sqlite3
from datetime import datetime
from itertools import groupby
from zoneinfo import ZoneInfo

_POST_ID_RE = re.compile(r'(post_id=)(\d+)', re.IGNORECASE)

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

_WARSAW = ZoneInfo("Europe/Warsaw")

from board.models import Forum, Post, Topic, User, TopicParticipant
from board.bbcode_lint import repair as repair_bbcode
from board.management.commands.update_forum_counts import compute_recursive_counts
from board.quote_refs import rebuild_quote_references_for_posts
from board.search_index import rebuild_post_search_index_for_posts


TOPIC_TYPE_MAP = {
    "":            Topic.TopicType.NORMAL,
    "Przyklejony": Topic.TopicType.STICKY,
    "Ogłoszenie":  Topic.TopicType.ANNOUNCEMENT,
    "[ Ankieta ]": Topic.TopicType.NORMAL,   # polls → treat as normal for now
    "Przesunięty": Topic.TopicType.NORMAL,   # moved placeholder
}

# Polish month abbreviations used by phpBB/sfinia
_PL_MONTHS = {
    "Sty": 1, "Lut": 2, "Mar": 3, "Kwi": 4, "Maj": 5, "Cze": 6,
    "Lip": 7, "Sie": 8, "Wrz": 9, "Paź": 10, "Lis": 11, "Gru": 12,
}


def parse_pl_date(s):
    """Parse 'Nie 21:08, 22 Sty 2006' → aware datetime (UTC).

    Interprets the time as Europe/Warsaw (handles DST automatically:
    winter = UTC+1, summer = UTC+2). Returns None on failure.
    """
    if not s:
        return None
    try:
        # Format: <DayAbbr> <HH:MM>, <DD> <MonthAbbr> <YYYY>
        parts = s.split()
        # parts: ['Nie', '21:08,', '22', 'Sty', '2006']
        time_part = parts[1].rstrip(",")
        hour, minute = map(int, time_part.split(":"))
        day = int(parts[2])
        month = _PL_MONTHS.get(parts[3])
        year = int(parts[4])
        if month is None:
            return None
        # Create naive datetime, then attach Warsaw zone — DST handled automatically
        naive = datetime(year, month, day, hour, minute)
        return naive.replace(tzinfo=_WARSAW)
    except Exception:
        return None


class Command(BaseCommand):
    help = "Import posts from sfiniabb.db with flexible sampling"

    def add_arguments(self, parser):
        parser.add_argument("archive_db", help="Path to sfiniabb.db")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--first",  type=int, metavar="N", help="First N posts")
        group.add_argument("--last",   type=int, metavar="N", help="Last N posts")
        group.add_argument("--every",  type=int, metavar="N", help="Every N-th post")
        group.add_argument("--random", type=int, metavar="N", help="N random posts")

    def handle(self, *args, **options):
        db_path = options["archive_db"]
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        # --- Build post query based on sampling mode ---
        if options["first"]:
            sql = (
                "SELECT p.*, t.title as topic_title, t.forum_id, t.topic_type, "
                "       t.view_count, t.author_name as topic_author_name "
                "FROM posts p JOIN topics t ON p.topic_id = t.topic_id "
                "WHERE p.post_id IN (SELECT post_id FROM posts ORDER BY post_id LIMIT ?) "
                "ORDER BY p.topic_id, p.post_order"
            )
            params = (options["first"],)

        elif options["last"]:
            sql = (
                "SELECT p.*, t.title as topic_title, t.forum_id, t.topic_type, "
                "       t.view_count, t.author_name as topic_author_name "
                "FROM posts p JOIN topics t ON p.topic_id = t.topic_id "
                "WHERE p.post_id IN (SELECT post_id FROM posts ORDER BY post_id DESC LIMIT ?) "
                "ORDER BY p.topic_id, p.post_order"
            )
            params = (options["last"],)

        elif options["every"]:
            n = options["every"]
            sql = (
                "SELECT p.*, t.title as topic_title, t.forum_id, t.topic_type, "
                "       t.view_count, t.author_name as topic_author_name "
                "FROM posts p JOIN topics t ON p.topic_id = t.topic_id "
                f"WHERE p.post_id IN (SELECT post_id FROM posts WHERE (post_id % {n}) = 0) "
                "ORDER BY p.topic_id, p.post_order"
            )
            params = ()

        elif options["random"]:
            sql = (
                "SELECT p.*, t.title as topic_title, t.forum_id, t.topic_type, "
                "       t.view_count, t.author_name as topic_author_name "
                "FROM posts p JOIN topics t ON p.topic_id = t.topic_id "
                "WHERE p.post_id IN (SELECT post_id FROM posts ORDER BY RANDOM() LIMIT ?) "
                "ORDER BY p.topic_id, p.post_order"
            )
            params = (options["random"],)

        else:
            sql = (
                "SELECT p.*, t.title as topic_title, t.forum_id, t.topic_type, "
                "       t.view_count, t.author_name as topic_author_name "
                "FROM posts p JOIN topics t ON p.topic_id = t.topic_id "
                "ORDER BY p.topic_id, p.post_order"
            )
            params = ()

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        self.stdout.write(f"Pobrano {len(rows)} postów z archiwum.")

        # --- Pre-load lookup tables ---
        user_map  = {u.username: u for u in User.objects.all()}
        topic_map = {}   # archive topic_id → our Topic instance

        # Build archive_forum_id → our Forum mapping by title
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        archive_forums = {
            r["forum_id"]: r["title"]
            for r in conn2.execute("SELECT forum_id, title FROM forums").fetchall()
        }
        conn2.close()

        our_forums_by_title = {f.title: f for f in Forum.objects.all()}
        forum_map = {}
        for arch_id, title in archive_forums.items():
            if title in our_forums_by_title:
                forum_map[arch_id] = our_forums_by_title[title]
        self.stdout.write(
            f"Zmapowano {len(forum_map)}/{len(archive_forums)} forów po tytułach."
        )

        posts_created = topics_created = skipped_forum = repaired_posts = 0
        sfinia_to_django = {}  # sfinia post_id (int) → django post_id (int)
        imported_post_ids = []
        imported_topic_ids = []

        # --- Group by topic_id (rows already sorted by topic_id, post_order) ---
        with transaction.atomic():
            for archive_topic_id, group in groupby(rows, key=lambda r: r["topic_id"]):
                posts_in_topic = list(group)
                first = posts_in_topic[0]
                last  = posts_in_topic[-1]

                # Get or create Topic
                if archive_topic_id not in topic_map:
                    forum = forum_map.get(first["forum_id"])
                    if forum is None:
                        skipped_forum += len(posts_in_topic)
                        continue

                    topic_author = user_map.get(first["topic_author_name"]) or user_map.get(first["author_name"])
                    topic_type   = TOPIC_TYPE_MAP.get(
                        first["topic_type"] or "", Topic.TopicType.NORMAL
                    )
                    first_dt = parse_pl_date(first["created_at"])

                    topic = Topic.objects.create(
                        forum=forum,
                        archive_topic_id=archive_topic_id,
                        title=first["topic_title"],
                        author=topic_author,
                        topic_type=topic_type,
                        view_count=first["view_count"] or 0,
                    )
                    if first_dt:
                        topic.created_at = first_dt
                        topic.save(update_fields=["created_at"])
                    topic_map[archive_topic_id] = topic
                    topics_created += 1
                    imported_topic_ids.append(topic.pk)
                else:
                    topic = topic_map[archive_topic_id]   # shouldn't happen (one group per id)

                # Re-number posts 1, 2, 3… within this topic
                post_objects = []
                sfinia_ids = []   # parallel to post_objects
                for new_order, row in enumerate(posts_in_topic, start=1):
                    author = user_map.get(row["author_name"])
                    keys = row.keys()
                    # Use enriched content_quotes when available, fall back to content.
                    # When quote_status=4 the post has unbalanced tags — content_quotes
                    # is NULL there, so content is used and broken_tags is set True.
                    content_quotes = row["content_quotes"] if "content_quotes" in keys else None
                    content_bbcode = content_quotes or row["content"] or ""
                    repaired_content, repair_changes = repair_bbcode(content_bbcode)
                    if repair_changes:
                        content_bbcode = repaired_content
                        repaired_posts += 1
                    broken_tags = (
                        "quote_status" in keys and row["quote_status"] == 4
                    )
                    dt = parse_pl_date(row["created_at"])
                    post_objects.append(Post(
                        topic=topic,
                        archive_post_id=row["post_id"],
                        author=author,
                        subject=row["subject"] or "",
                        content_bbcode=content_bbcode,
                        broken_tags=broken_tags,
                        post_order=new_order,
                        **({"created_at": dt} if dt else {}),
                    ))
                    sfinia_ids.append(row["post_id"])
                    posts_created += 1

                Post.objects.bulk_create(post_objects)
                # bulk_create sets pk on objects (Django 4.1+, SQLite 3.35+)
                for sfinia_id, post_obj in zip(sfinia_ids, post_objects):
                    if post_obj.pk:
                        sfinia_to_django[sfinia_id] = post_obj.pk
                        imported_post_ids.append(post_obj.pk)

                # Update topic reply_count and last_post_at
                total = len(post_objects)
                topic.reply_count = max(total - 1, 0)
                last_dt = parse_pl_date(last["created_at"])
                update_fields = ["reply_count"]
                if last_dt:
                    topic.last_post_at = last_dt
                    update_fields.append("last_post_at")
                topic.save(update_fields=update_fields)

        # --- Remap sfinia post_ids in content_bbcode → django post_ids ---
        if sfinia_to_django:
            self.stdout.write("ReMapuję post_id w cytatach (sfinia → django)…")

            def _remap(content):
                def sub(m):
                    old = int(m.group(2))
                    new = sfinia_to_django.get(old)
                    return f'{m.group(1)}{new}' if new else m.group(0)
                return _POST_ID_RE.sub(sub, content)

            batch = []
            remapped = 0
            for post in Post.objects.only("pk", "content_bbcode").iterator(chunk_size=2000):
                new_content = _remap(post.content_bbcode)
                if new_content != post.content_bbcode:
                    post.content_bbcode = new_content
                    batch.append(post)
                if len(batch) >= 1000:
                    Post.objects.bulk_update(batch, ["content_bbcode"])
                    remapped += len(batch)
                    batch = []
            if batch:
                Post.objects.bulk_update(batch, ["content_bbcode"])
                remapped += len(batch)
            self.stdout.write(f"  Zaktualizowano {remapped} postów.")

        if imported_post_ids:
            self.stdout.write("Buduję indeks cytowań (forum_quote_refs)…")
            indexed = rebuild_quote_references_for_posts(
                Post.objects.filter(pk__in=imported_post_ids).only("pk", "content_bbcode")
            )
            self.stdout.write(f"  Zindeksowano {indexed} postów.")

            self.stdout.write("Buduję indeks wyszukiwania (forum_post_search)…")
            indexed_search = rebuild_post_search_index_for_posts(
                Post.objects.filter(pk__in=imported_post_ids).select_related("topic", "topic__forum", "author")
            )
            self.stdout.write(f"  Zindeksowano wyszukiwanie dla {indexed_search} postów.")

            if imported_topic_ids:
                self.stdout.write("Buduję uczestników wątków…")
                TopicParticipant.objects.filter(topic_id__in=imported_topic_ids).delete()
                participant_rows = (
                    Post.objects.filter(topic_id__in=imported_topic_ids, author__isnull=False)
                    .values("topic_id", "author_id")
                    .annotate(post_count=Count("id"), last_post_at=Max("created_at"))
                    .order_by()
                )
                TopicParticipant.objects.bulk_create([
                    TopicParticipant(
                        topic_id=row["topic_id"],
                        user_id=row["author_id"],
                        post_count=row["post_count"],
                        last_post_at=row["last_post_at"],
                    )
                    for row in participant_rows
                ], batch_size=1000)
                self.stdout.write("  Zbudowano uczestników wątków.")

        # --- Set topic.last_post FK ---
        self.stdout.write("Ustawiam last_post na wątkach…")
        for topic in Topic.objects.prefetch_related("posts"):
            last_post = topic.posts.order_by("post_order").last()
            if last_post:
                topic.last_post = last_post
                topic.save(update_fields=["last_post"])

        # --- Update forum counters (recursive, like phpBB) ---
        self.stdout.write("Aktualizuję liczniki forów (rekurencyjnie)…")
        totals = compute_recursive_counts()
        for forum in Forum.objects.all():
            tc, pc = totals[forum.id]
            forum.topic_count = tc
            forum.post_count  = pc
            forum.save(update_fields=["topic_count", "post_count"])

        # --- Recalculate per-user post counters from imported posts ---
        self.stdout.write("Przeliczam liczbę postów użytkowników…")
        User.objects.update(post_count=0)
        user_post_counts = Post.objects.exclude(author__isnull=True).values("author_id").annotate(
            total=Count("id")
        )
        users_to_update = []
        for row in user_post_counts.iterator():
            users_to_update.append(
                User(id=row["author_id"], post_count=row["total"])
            )
        if users_to_update:
            User.objects.bulk_update(users_to_update, ["post_count"], batch_size=1000)
        self.stdout.write(f"  Zaktualizowano liczniki dla {len(users_to_update)} użytkowników.")

        self.stdout.write(self.style.SUCCESS(
            f"Gotowe. Wątki: {topics_created}, Posty: {posts_created}"
            + (f", Naprawione BBCode/linki: {repaired_posts}" if repaired_posts else "")
            + (f", Pominięte (brak forum): {skipped_forum}" if skipped_forum else "")
        ))
