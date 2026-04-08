"""
Recompute forum topic_count and post_count recursively (includes subforums).

Usage:
    python manage.py update_forum_counts

phpBB convention: a forum's counters include all nested subforums.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from board.models import Forum, Post


def compute_recursive_last_posts():
    """Return {forum_id: Post|None} — last post recursively including subforums."""
    forums = list(Forum.objects.all())
    children = defaultdict(list)
    for f in forums:
        if f.parent_id:
            children[f.parent_id].append(f.id)

    # Direct last post per forum (single query per forum, but run once at import)
    direct = {}
    for f in forums:
        post = (
            Post.objects.filter(topic__forum_id=f.id)
            .order_by("-created_at")
            .select_related("author")
            .first()
        )
        direct[f.id] = post

    memo = {}

    def recursive(fid):
        if fid in memo:
            return memo[fid]
        result = direct[fid]
        for child_id in children[fid]:
            child = recursive(child_id)
            if child and (result is None or child.created_at > result.created_at):
                result = child
        memo[fid] = result
        return result

    return {f.id: recursive(f.id) for f in forums}


def compute_recursive_counts():
    """Return {forum_id: (total_topic_count, total_post_count)} including subforums."""
    forums = list(Forum.objects.all())

    # Direct counts
    direct_topics = {f.id: f.topics.count() for f in forums}
    direct_posts  = {
        f.id: Post.objects.filter(topic__forum_id=f.id).count()
        for f in forums
    }

    # Children mapping
    children = defaultdict(list)
    for f in forums:
        if f.parent_id:
            children[f.parent_id].append(f.id)

    # Recursive sum (max depth 3 — safe for simple recursion)
    def recursive(fid):
        t = direct_topics[fid]
        p = direct_posts[fid]
        for child_id in children[fid]:
            ct, cp = recursive(child_id)
            t += ct
            p += cp
        return t, p

    return {f.id: recursive(f.id) for f in forums}


class Command(BaseCommand):
    help = "Recompute forum counters recursively (includes subforums, like phpBB)"

    def handle(self, *args, **options):
        totals = compute_recursive_counts()
        last_posts = compute_recursive_last_posts()
        updated = 0
        for forum in Forum.objects.all():
            tc, pc = totals[forum.id]
            lp = last_posts.get(forum.id)
            lp_at = lp.created_at if lp else None
            forum.topic_count  = tc
            forum.post_count   = pc
            forum.last_post    = lp
            forum.last_post_at = lp_at
            forum.save(update_fields=["topic_count", "post_count", "last_post", "last_post_at"])
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Zaktualizowano {updated} forów."
        ))
