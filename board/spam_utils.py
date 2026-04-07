"""
Spam filtering helpers — used in views to hide posts/forums from spammers.

Rules:
  anonymous / NORMAL (spam_class=0): hide GRAY and WEB by default (can toggle via settings)
  GRAY (spam_class=1):               hide WEB, always see own class
  WEB  (spam_class=2):               see everyone — they're in the worst group themselves

Forum visibility:
  archive_level=0 (NORMAL): everyone including anonymous
  archive_level=1 (SOFT):   spam_class >= 1 (GRAY + WEB)
  archive_level=2 (HARD):   spam_class >= 2 (WEB only)
"""

from django.db.models import Q

from .models import IgnoredUser, User


def get_user_spam_class(user) -> int:
    """Return user's spam_class, or 0 for anonymous."""
    if user.is_authenticated:
        return user.spam_class
    return 0


def get_author_spam_filter(user) -> Q:
    """Return Q that excludes posts/topics from spam authors the user should not see.

    Apply to any Post or Topic queryset:
        Post.objects.filter(get_author_spam_filter(request.user))
        Topic.objects.filter(get_author_spam_filter(request.user))
    """
    spam_class = get_user_spam_class(user)

    ignored_ids = get_ignored_user_ids(user)
    hidden_q = Q()
    if ignored_ids:
        hidden_q |= Q(author_id__in=ignored_ids)

    if spam_class >= User.SpamClass.WEB:
        # WEB users see everyone except explicitly ignored users
        return ~hidden_q if hidden_q else Q()

    if spam_class == User.SpamClass.GRAY:
        # GRAY users see their own class but not WEB
        hidden_q |= Q(author__spam_class=User.SpamClass.WEB)
        return ~hidden_q if hidden_q else Q()

    # NORMAL or anonymous — check personal settings (default: hide both classes)
    hidden = _get_hidden_classes_for_normal(user)
    if hidden:
        hidden_q |= Q(author__spam_class__in=hidden)
    return ~hidden_q if hidden_q else Q()


def get_topic_visibility_filter(user) -> tuple[str, Q]:
    """Return (last_post_at_field, exclude_q) for filtering topic lists by visibility.

    Uses the denormalized last_post_at_<class> columns on Topic so we don't have
    to join Post + User. The returned field name is the timestamp column to
    filter on (e.g. ``last_post_at_normal__gt=...``); the Q applies an additional
    exclude for individually ignored users (matched against the matching
    last_post_<class>_author_id column).
    """
    spam_class = get_user_spam_class(user)
    if spam_class >= User.SpamClass.WEB:
        ts_field = "last_post_at"
        author_field = None
    elif spam_class == User.SpamClass.GRAY:
        ts_field = "last_post_at_gray"
        author_field = "last_post_gray_author_id"
    else:
        # NORMAL or anonymous — also obey personal hidden-class settings
        hidden = _get_hidden_classes_for_normal(user)
        if User.SpamClass.GRAY in hidden:
            ts_field = "last_post_at_normal"
            author_field = "last_post_normal_author_id"
        elif User.SpamClass.WEB in hidden:
            ts_field = "last_post_at_gray"
            author_field = "last_post_gray_author_id"
        else:
            ts_field = "last_post_at"
            author_field = None

    ignored_ids = get_ignored_user_ids(user)
    exclude_q = Q()
    if ignored_ids and author_field:
        exclude_q = Q(**{f"{author_field}__in": ignored_ids})
    return ts_field, exclude_q


def get_ignored_user_ids(user) -> set[int]:
    if not getattr(user, "is_authenticated", False):
        return set()
    return set(
        IgnoredUser.objects.filter(owner=user).values_list("ignored_user_id", flat=True)
    )


def _get_hidden_classes_for_normal(user) -> list:
    """Return list of spam_class values hidden for a NORMAL/anonymous user."""
    if not user.is_authenticated:
        return [User.SpamClass.GRAY, User.SpamClass.WEB]

    try:
        s = user.ignore_settings
        hidden = []
        if s.hide_gray:
            hidden.append(User.SpamClass.GRAY)
        if s.hide_web:
            hidden.append(User.SpamClass.WEB)
        return hidden
    except Exception:
        # No settings object yet — use defaults
        return [User.SpamClass.GRAY, User.SpamClass.WEB]


def get_max_forum_level(user) -> int:
    """Return maximum archive_level of forums this user may see."""
    return get_user_spam_class(user)


def filter_forums(forums_qs, user):
    """Filter a Forum queryset to only forums the user may access."""
    max_level = get_max_forum_level(user)
    return forums_qs.filter(archive_level__lte=max_level)
