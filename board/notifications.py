"""
Helper functions that create Notification rows.

All functions are safe to call unconditionally — they skip silently when
the notification would make no sense (e.g. actor == recipient).
"""
from __future__ import annotations
from django.db import models as django_models


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _create(notif_type, *, recipient=None, recipient_id=None, actor=None, post=None, pm=None):
    from .models import Notification
    kwargs = dict(notif_type=notif_type, actor=actor, post=post, pm=pm)
    if recipient is not None:
        kwargs["recipient"] = recipient
    else:
        kwargs["recipient_id"] = recipient_id
    Notification.objects.create(**kwargs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_quote_reply(post):
    """
    Create QUOTE_REPLY notifications for all users quoted in *post*.

    Called after a post is created (and is_pending=False) so that
    QuoteReferences already exist for it.
    Skips: quoting yourself, quoting a deleted/missing author.
    """
    from .models import Notification, QuoteReference

    quoted_author_ids = (
        QuoteReference.objects
        .filter(citing_post=post)
        .exclude(quoted_post__author__isnull=True)
        .values_list("quoted_post__author_id", flat=True)
        .distinct()
    )
    for uid in quoted_author_ids:
        if uid == post.author_id:
            continue
        _create(
            notif_type=Notification.Type.QUOTE_REPLY,
            recipient_id=uid,
            actor=post.author,
            post=post,
        )


def notify_post_liked(post, actor):
    """Create POST_LIKED notification for post author."""
    from .models import Notification
    if not post.author_id or post.author_id == actor.pk:
        return
    _create(
        notif_type=Notification.Type.POST_LIKED,
        recipient=post.author,
        actor=actor,
        post=post,
    )


def notify_post_unliked(post, actor):
    """Create POST_UNLIKED notification for post author."""
    from .models import Notification
    if not post.author_id or post.author_id == actor.pk:
        return
    _create(
        notif_type=Notification.Type.POST_UNLIKED,
        recipient=post.author,
        actor=actor,
        post=post,
    )


def notify_pending_queue():
    """
    Ensure every moderator/admin/root has one unread PENDING_QUEUE notification.
    Creates a new one only if none already exists (unread).
    """
    from .models import Notification, User
    mods = User.objects.filter(
        django_models.Q(role__gte=User.ROLE_MODERATOR) | django_models.Q(is_root=True),
        is_active=True,
    )
    for mod in mods:
        exists = Notification.objects.filter(
            recipient=mod,
            notif_type=Notification.Type.PENDING_QUEUE,
            is_read=False,
        ).exists()
        if not exists:
            _create(notif_type=Notification.Type.PENDING_QUEUE, recipient=mod)
