"""
Recompute forum topic_count and post_count recursively (includes subforums).

Usage:
    python manage.py update_forum_counts

phpBB convention: a forum's counters include all nested subforums.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from board.models import Forum, Post


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
        updated = 0
        for forum in Forum.objects.all():
            tc, pc = totals[forum.id]
            if forum.topic_count != tc or forum.post_count != pc:
                forum.topic_count = tc
                forum.post_count  = pc
                forum.save(update_fields=["topic_count", "post_count"])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Zaktualizowano {updated} forów."
        ))
