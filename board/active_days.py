"""Helpers to maintain User.active_days incrementally.

active_days = number of distinct UTC dates on which a user has at least one post.

Rules:
- Adding a post on a day the user has NO other posts → active_days += 1
- Deleting a post that was the ONLY post that day  → active_days -= 1
- Mass deletions (delete_user_and_cleanup, bulk spam cleanup)
  do NOT call these per-post — recalculate or skip as appropriate.
"""

from django.db.models import F


def increment_if_new_day(user, post) -> None:
    """Call after creating `post`. Increments active_days if this is user's first post that UTC day."""
    if not user or not user.pk:
        return
    from board.models import Post
    post_date = post.created_at.date()
    already = Post.objects.filter(
        author_id=user.pk,
        created_at__date=post_date,
    ).exclude(pk=post.pk).exists()
    if not already:
        type(user).objects.filter(pk=user.pk).update(active_days=F("active_days") + 1)


def decrement_if_last_on_day(user, post) -> None:
    """Call before deleting `post`. Decrements active_days if this was user's only post that UTC day."""
    if not user or not user.pk:
        return
    from board.models import Post
    post_date = post.created_at.date()
    other = Post.objects.filter(
        author_id=user.pk,
        created_at__date=post_date,
    ).exclude(pk=post.pk).exists()
    if not other:
        type(user).objects.filter(pk=user.pk, active_days__gt=0).update(
            active_days=F("active_days") - 1
        )


def recalculate_for_user(user) -> int:
    """Full recalculation for one user. Returns new value. Use after bulk ops on a single user."""
    from board.models import Post
    from django.db.models.functions import TruncDate
    days = (
        Post.objects
        .filter(author_id=user.pk)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .distinct()
        .count()
    )
    type(user).objects.filter(pk=user.pk).update(active_days=days)
    return days
