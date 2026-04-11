"""Delete a user account with full cleanup:
- rewrite quotes in other posts (like rename, but to "[usunięty]")
- enriched quotes pointing to deleted posts are stripped to plain [quote="[usunięty]"]
- all user's posts are deleted with proper topic/forum stat updates
- user account is deleted (nick freed)
"""

import re
from django.db import transaction
from django.db.models import F, Q

from .models import Post, QuoteReference, Topic, Forum, User
from .quote_refs import rebuild_quote_references_for_posts
from .user_lock import user_processing_lock


DELETED_LABEL = "[usunięty]"

_ANY_ENRICHED_QUOTE_RE = re.compile(
    r'\[(?P<qtype>f?quote)(?:="(?P<author>[^"]*)")?(?P<mid>\s+post_id=(?P<post_id>\d+))(?P<tail>[^\]]*)\]',
    re.IGNORECASE,
)

_NAMED_QUOTE_RE = re.compile(
    r'\[(?P<qtype>f?quote)="(?P<author>[^"]*)"\]',
    re.IGNORECASE,
)


def _rewrite_for_deletion(content: str, old_username: str, deleted_post_ids: frozenset[int]) -> tuple[str, int]:
    """Rewrite all quotes referencing old_username or deleted_post_ids.

    Named quotes:   [quote="nick"] → [quote="[usunięty]"]
    Enriched quotes pointing to deleted posts → [quote="[usunięty]"] (stripped)
    Returns (new_content, changed_count).
    """
    changed = 0

    # First pass: enriched quotes (have post_id attribute)
    def repl_enriched(match):
        nonlocal changed
        author = (match.group("author") or "").strip()
        post_id = int(match.group("post_id"))
        if author != old_username and post_id not in deleted_post_ids:
            return match.group(0)
        changed += 1
        return f'[{match.group("qtype")}="{DELETED_LABEL}"]'

    content = _ANY_ENRICHED_QUOTE_RE.sub(repl_enriched, content)

    # Second pass: plain named quotes (no post_id)
    def repl_named(match):
        nonlocal changed
        if match.group("author") != old_username:
            return match.group(0)
        changed += 1
        return f'[{match.group("qtype")}="{DELETED_LABEL}"]'

    content = _NAMED_QUOTE_RE.sub(repl_named, content)

    return content, changed


def delete_user_and_cleanup(user: User) -> dict:
    """Full user deletion with quote rewriting and post removal."""
    old_username = user.username
    user_post_ids = frozenset(Post.objects.filter(author=user).values_list("pk", flat=True))

    # Find posts by OTHER authors that quote this user
    quote_post_ids = list(
        QuoteReference.objects.filter(
            Q(quoted_username=old_username) |
            Q(source_post_id__in=user_post_ids)
        )
        .exclude(post__author=user)
        .values_list("post_id", flat=True)
        .distinct()
    )

    posts_changed = 0
    tags_changed = 0
    batch = []
    changed_post_ids = []

    with user_processing_lock(user), transaction.atomic():
        # 1. Rewrite quotes in other posts
        posts_qs = (
            Post.objects.filter(pk__in=quote_post_ids)
            .only("pk", "content_bbcode")
            .order_by("pk")
        )
        for post in posts_qs.iterator(chunk_size=500):
            new_content, n_changed = _rewrite_for_deletion(
                post.content_bbcode, old_username, user_post_ids
            )
            if not n_changed:
                continue
            post.content_bbcode = new_content
            batch.append(post)
            changed_post_ids.append(post.pk)
            posts_changed += 1
            tags_changed += n_changed
            if len(batch) >= 500:
                Post.objects.bulk_update(batch, ["content_bbcode"])
                batch.clear()
        if batch:
            Post.objects.bulk_update(batch, ["content_bbcode"])
        if changed_post_ids:
            rebuild_quote_references_for_posts(
                Post.objects.filter(pk__in=changed_post_ids).only("pk", "content_bbcode")
            )

        # 2. Delete user's posts and update stats
        affected_topic_ids = list(
            Post.objects.filter(author=user).values_list("topic_id", flat=True).distinct()
        )

        Post.objects.filter(author=user).delete()

        # Delete empty topics, renumber and update stats for the rest
        affected_forum_ids = set()
        for topic in Topic.objects.filter(pk__in=affected_topic_ids).select_related("forum"):
            affected_forum_ids.add(topic.forum_id)
            if topic.posts.count() == 0:
                topic.delete()
                continue
            # Renumber remaining posts sequentially
            for idx, p in enumerate(
                topic.posts.order_by("created_at").values_list("pk", flat=True), start=1
            ):
                Post.objects.filter(pk=p).update(post_order=idx)
            remaining = topic.posts.count()
            last_post = topic.posts.order_by("-created_at").first()
            topic.reply_count = max(0, remaining - 1)
            topic.last_post = last_post
            topic.last_post_at = last_post.created_at if last_post else None
            topic.save(update_fields=["reply_count", "last_post", "last_post_at"])

        # Update forum stats
        for forum in Forum.objects.filter(pk__in=affected_forum_ids):
            forum.post_count = Post.objects.filter(topic__forum=forum).count()
            forum.topic_count = forum.topics.count()
            last = Post.objects.filter(topic__forum=forum).order_by("-created_at").first()
            forum.last_post = last
            forum.last_post_at = last.created_at if last else None
            forum.save(update_fields=["post_count", "topic_count", "last_post", "last_post_at"])

        # 3. Delete the user (SET_NULL on remaining post.author handled by DB)
        user.delete()

    return {
        "old_username": old_username,
        "posts_deleted": len(user_post_ids),
        "quote_posts_changed": posts_changed,
        "tags_changed": tags_changed,
    }
