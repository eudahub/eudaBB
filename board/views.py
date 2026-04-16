import math
import secrets
import re
from html import escape
from datetime import datetime, time, timedelta

from django.db import models as django_models
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.core.mail import send_mail
import datetime as _dt
from django.core.paginator import Paginator
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.conf import settings

from .models import (
    Section, Forum, Topic, Post, User, ActivationToken, BlockedIP,
    PasswordResetCode, PrivateMessage, PrivateMessageBox, PostLike,
    PostSearchIndex, SiteConfig, Poll, PollOption, PollVote,
    TopicParticipant, TopicReadState, IgnoredUser,
    Checklist, ChecklistCategory, ChecklistItem, ChecklistUpvote, ChecklistComment,
)
from .forms import (
    RegisterForm, RegisterStartForm, RegisterFinishForm,
    NewTopicForm, ReplyForm, validate_post_content, validate_pm_content,
)
from .email_utils import mask_email, mask_email_variants
from .spam_utils import (
    get_author_spam_filter,
    filter_forums,
    get_ignored_user_ids,
    get_topic_visibility_filter,
)
from .middleware import invalidate_blocked_ips_cache
from .auth_utils import prehash_password
from .username_utils import normalize
from .user_rename import rename_user_and_update_quotes
from .quote_refs import rebuild_quote_references_for_post
from .quote_selection import extract_exact_quote_fragment, normalize_selected_text
from .search_index import extract_author_search_text, expand_morph_term, expand_morph_term_all, normalize_search_text, strip_diacritics


# ---------------------------------------------------------------------------
# Stat helpers — keep view functions small
# ---------------------------------------------------------------------------

def _update_topic_stats(topic: Topic, last_post: Post) -> None:
    """Recalculate and save cached counters on a topic after a new post."""
    topic.reply_count = topic.posts.filter(is_pending=False).count() - 1  # first post is not a "reply"
    topic.last_post = last_post
    topic.last_post_at = last_post.created_at

    # Maintain denormalized last_post_at_<class> used by "Nowe wątki" / "Nowe posty"
    # to filter out spam classes without joining Post + User.
    update_fields = ["reply_count", "last_post", "last_post_at"]
    author = last_post.author
    spam_class = author.spam_class if author is not None else User.SpamClass.NORMAL
    if spam_class <= User.SpamClass.NORMAL:
        topic.last_post_at_normal = last_post.created_at
        topic.last_post_normal_author_id = author.pk if author else None
        update_fields += ["last_post_at_normal", "last_post_normal_author_id"]
    if spam_class <= User.SpamClass.GRAY:
        topic.last_post_at_gray = last_post.created_at
        topic.last_post_gray_author_id = author.pk if author else None
        update_fields += ["last_post_at_gray", "last_post_gray_author_id"]

    topic.save(update_fields=update_fields)


def _update_forum_stats(forum: Forum, last_post: Post) -> None:
    """Recalculate and save cached counters on a forum after a new post."""
    forum.post_count = Post.objects.filter(topic__forum=forum, is_pending=False).count()
    forum.topic_count = forum.topics.filter(is_pending=False).count()
    forum.last_post = last_post
    forum.last_post_at = last_post.created_at
    forum.save(update_fields=["post_count", "topic_count", "last_post", "last_post_at"])


def _increment_user_post_count(user) -> None:
    """Increment post counter on user model."""
    if user and user.is_authenticated:
        user.post_count += 1
        user.save(update_fields=["post_count"])


def _increment_topic_participant(topic: Topic, author, post: Post) -> None:
    if not author or not getattr(author, "pk", None):
        return
    participant, created = TopicParticipant.objects.get_or_create(
        topic=topic,
        user=author,
        defaults={
            "post_count": 1,
            "last_post_at": post.created_at,
        },
    )
    if not created:
        participant.post_count += 1
        if participant.last_post_at is None or post.created_at > participant.last_post_at:
            participant.last_post_at = post.created_at
        participant.save(update_fields=["post_count", "last_post_at"])


def _get_or_build_topic_participants(topic: Topic):
    participants = list(
        topic.participants.select_related("user").order_by("-post_count", "user__username", "pk")
    )
    if participants:
        return participants

    rows = list(
        topic.posts.filter(author__isnull=False)
        .values("author_id")
        .annotate(
            post_count=django_models.Count("id"),
            last_post_at=django_models.Max("created_at"),
        )
        .order_by("-post_count", "author_id")
    )
    if not rows:
        return []

    user_map = {
        user.pk: user
        for user in User.objects.filter(pk__in=[row["author_id"] for row in rows])
    }
    TopicParticipant.objects.bulk_create([
        TopicParticipant(
            topic=topic,
            user_id=row["author_id"],
            post_count=row["post_count"],
            last_post_at=row["last_post_at"],
        )
        for row in rows
        if row["author_id"] in user_map
    ], ignore_conflicts=True)
    return list(
        topic.participants.select_related("user").order_by("-post_count", "user__username", "pk")
    )


def _update_topic_read_state(user, topic: Topic, page) -> None:
    if not getattr(user, "is_authenticated", False):
        return
    if getattr(user, "is_root", False):
        return
    object_list = list(page.object_list) if page is not None else []
    if not object_list:
        return
    max_read_order = max(post.post_order for post in object_list)
    state, created = TopicReadState.objects.get_or_create(
        user=user,
        topic=topic,
        defaults={
            "last_read_post_order": max_read_order,
            "last_read_at": timezone.now(),
        },
    )
    if not created and max_read_order > state.last_read_post_order:
        state.last_read_post_order = max_read_order
        state.last_read_at = timezone.now()
        state.save(update_fields=["last_read_post_order", "last_read_at"])


def _build_unread_topic_url(user, topic: Topic, read_state, posts_per_page: int) -> str:
    last_post = topic.last_post
    if last_post is None:
        return reverse("topic_detail", args=[topic.pk])

    first_unread_post = None
    if read_state is not None and read_state.last_read_post_order:
        first_unread_post = (
            topic.posts.filter(post_order__gt=read_state.last_read_post_order)
            .order_by("post_order")
            .only("pk", "post_order")
            .first()
        )
    else:
        baseline = getattr(user, "mark_all_read_at", None)
        if baseline is not None:
            first_unread_post = (
                topic.posts.filter(created_at__gt=baseline)
                .order_by("post_order")
                .only("pk", "post_order")
                .first()
            )

    if first_unread_post is None:
        page_num = ((last_post.post_order - 1) // posts_per_page) + 1
        return f'{reverse("topic_detail", args=[topic.pk])}?page={page_num}#post-{last_post.pk}'

    page_num = ((first_unread_post.post_order - 1) // posts_per_page) + 1
    return f'{reverse("topic_detail", args=[topic.pk])}?page={page_num}#post-{first_unread_post.pk}'


def _annotate_topics_with_unread_state(user, topics, posts_per_page: int):
    topics = list(topics)
    if not getattr(user, "is_authenticated", False):
        for topic in topics:
            topic.has_unread = False
            topic.unread_url = reverse("topic_detail", args=[topic.pk])
        return topics

    topic_ids = [topic.pk for topic in topics]
    read_state_map = {
        state.topic_id: state
        for state in TopicReadState.objects.filter(
            user=user,
            topic_id__in=topic_ids,
        )
    }
    baseline = user.mark_all_read_at

    for topic in topics:
        state = read_state_map.get(topic.pk)
        last_post = topic.last_post
        has_unread = False
        if last_post is not None:
            if state is not None:
                has_unread = state.last_read_post_order < last_post.post_order
            elif topic.last_post_at and topic.last_post_at > baseline:
                has_unread = True
        topic.has_unread = has_unread
        if has_unread:
            topic.unread_url = _build_unread_topic_url(user, topic, state, posts_per_page)
        else:
            topic.unread_url = reverse("topic_detail", args=[topic.pk])
    return topics


def _get_global_pinned_topic_posts(exclude_topic=None):
    qs = (
        Post.objects.select_related("author", "topic", "topic__forum")
        .filter(
            post_order=1,
            topic__topic_type__in=[
                Topic.TopicType.STICKY,
                Topic.TopicType.ANNOUNCEMENT,
            ],
        )
        .order_by("topic__forum__title", "-topic__topic_type", "topic__title", "topic_id")
    )
    if exclude_topic is not None:
        qs = qs.exclude(topic=exclude_topic)
    return qs[:55]


def _get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def _render_and_create_post(topic: Topic, author, content_bbcode: str,
                             post_order: int, author_ip: str = None,
                             is_temporary: bool = False,
                             is_pending: bool = False) -> Post:
    retain_until = _retain_until(flagged=False) if author_ip else None
    post = Post.objects.create(
        topic=topic,
        author=author,
        content_bbcode=content_bbcode,
        post_order=post_order,
        author_ip=author_ip,
        ip_retain_until=retain_until,
        is_temporary=is_temporary,
        is_pending=is_pending,
    )
    rebuild_quote_references_for_post(post)
    if author and not is_pending:
        from .active_days import increment_if_new_day
        increment_if_new_day(author, post)
    return post


def _is_temporary_content_mode():
    """Return True if new posts/topics should be created as temporary."""
    from .models import SiteConfig
    return SiteConfig.get().site_mode in (SiteConfig.MODE_MAINTENANCE, SiteConfig.MODE_BETA)


def can_convert_to_permanent(post):
    """Check if a temporary post can be converted to permanent.

    Returns (can_convert: bool, reason: str|None).
    Reasons: 'not_temporary', 'temporary_user', 'quotes_temporary',
             'feature_first_post'.
    """
    if not post.is_temporary:
        return False, "not_temporary"
    if post.author and post.author.is_temporary:
        return False, "temporary_user"
    from .models import QuoteReference
    if QuoteReference.objects.filter(
        post=post, source_post__is_temporary=True
    ).exists():
        return False, "quotes_temporary"
    # In a topic with a poll or checklist, the first post must be converted first.
    topic = post.topic
    if hasattr(topic, "poll") or hasattr(topic, "checklist"):
        first_post = topic.posts.order_by("post_order", "pk").first()
        if first_post and first_post.pk != post.pk and first_post.is_temporary:
            return False, "feature_first_post"
    return True, None


# ---------------------------------------------------------------------------
# Public views
# ---------------------------------------------------------------------------

def index(request):
    """Forum index: list all sections with their forums."""
    user_access = getattr(request.user, "archive_access", 0) if request.user.is_authenticated else 0
    is_staff = request.user.is_staff if request.user.is_authenticated else False
    sections = Section.objects.prefetch_related(
        "forums",
        "forums__last_post",
        "forums__last_post__author",
    ).all()
    # Filter forums per-section based on user's spam_class
    filtered_sections = []
    for section in sections:
        visible = list(filter_forums(section.forums.all(), request.user))
        if visible:
            section.visible_forums = visible
            filtered_sections.append(section)
    return render(request, "board/index.html", {
        "sections": filtered_sections,
        "user_access": user_access,
        "is_staff": is_staff,
    })


def forum_detail(request, forum_id):
    """Topic list for a single forum, paginated."""
    forum = get_object_or_404(Forum, pk=forum_id)
    from .spam_utils import get_max_forum_level
    if forum.archive_level > get_max_forum_level(request.user):
        return HttpResponseForbidden("Brak dostępu do tego forum.")
    ts_field, ignore_q = get_topic_visibility_filter(request.user)
    topics_qs = (
        forum.topics
        .select_related("author", "last_post", "last_post__author")
        .filter(**{f"{ts_field}__isnull": False})
        .exclude(ignore_q)
    )
    paginator = Paginator(topics_qs, getattr(settings, "TOPICS_PER_PAGE", 30))
    page = paginator.get_page(request.GET.get("page"))
    _annotate_topics_with_unread_state(
        request.user,
        page.object_list,
        getattr(settings, "POSTS_PER_PAGE", 20),
    )
    return render(request, "board/forum_detail.html", {"forum": forum, "page": page})


def topic_detail(request, topic_id):
    """Post list for a single topic, paginated. Increments view counter."""
    topic = get_object_or_404(
        Topic.objects.select_related("poll").prefetch_related("poll__options"),
        pk=topic_id,
    )

    # Increment view counter (simple version — no dedup)
    Topic.objects.filter(pk=topic_id).update(view_count=topic.view_count + 1)

    # Paginacja stabilna — wszystkie posty, niezależnie od PLONK; pending niewidoczne
    posts_qs = topic.posts.filter(is_pending=False).select_related("author", "updated_by")
    paginator = Paginator(posts_qs, getattr(settings, "POSTS_PER_PAGE", 20))
    page = paginator.get_page(request.GET.get("page"))
    _update_topic_read_state(request.user, topic, page)

    # Zbiór ID postów do ukrycia (spam) — template pokazuje placeholder zamiast treści
    spam_q = get_author_spam_filter(request.user)
    if spam_q:
        visible_post_ids = set(
            topic.posts.filter(spam_q).values_list("id", flat=True)
        )
    else:
        visible_post_ids = None  # None = pokaż wszystkie
    ignored_author_ids = get_ignored_user_ids(request.user)

    poll = getattr(topic, "poll", None)
    poll_now = timezone.now()
    poll_is_closed = False
    poll_user_votes = []
    poll_user_vote_option_ids = set()
    poll_can_vote = False
    poll_can_change_vote = False
    poll_show_results = False
    poll_max_option_votes = 0
    poll_days_left = None  # None = bezterminowa; int >= 0 = dni do końca

    if poll is not None:
        poll_is_closed = poll.is_closed or (poll.ends_at is not None and poll.ends_at <= poll_now)
        if poll.ends_at is not None and not poll_is_closed:
            poll_days_left = max(0, math.ceil((poll.ends_at - poll_now).total_seconds() / 86400))
        if request.user.is_authenticated and not poll.is_archived_import:
            poll_user_votes = list(
                PollVote.objects.filter(poll=poll, user=request.user).select_related("option")
            )
            poll_user_vote_option_ids = {vote.option_id for vote in poll_user_votes}
        poll_can_vote = (
            request.user.is_authenticated
            and not poll.is_archived_import
            and not poll_is_closed
            and (not poll_user_votes or poll.allow_vote_change)
        )
        poll_can_change_vote = poll_can_vote and bool(poll_user_votes) and poll.allow_vote_change
        poll_show_results = (
            poll.is_archived_import
            or poll.total_votes > 0
            or poll_is_closed
        )
        poll_options_list = list(poll.options.all())
        poll_max_option_votes = max((o.vote_count for o in poll_options_list), default=0)
        # Annotate each option with show_category=True when category header changes
        _prev_cat = object()
        for opt in poll_options_list:
            opt.show_category = (opt.category != _prev_cat)
            _prev_cat = opt.category

    reply_form = ReplyForm() if not topic.is_locked else None

    is_mod = (
        request.user.is_authenticated
        and _is_moderator(request.user, topic.forum)
    )
    is_admin_view = request.user.is_authenticated and request.user.role >= User.ROLE_ADMIN

    # Compute which posts on this page can be deleted/edited by current user.
    # First post (post_order=1) is only deletable when it's the sole post in topic.
    topic_post_count = topic.posts.count()
    deletable_post_ids = set()
    editable_post_ids = set()
    if request.user.is_authenticated:
        for p in page.object_list:
            author_role = p.author.role if p.author else User.ROLE_USER
            if is_admin_view:
                can_del = p.post_order > 1 or topic_post_count == 1
                deletable_post_ids.update([p.pk] if can_del else [])
                editable_post_ids.add(p.pk)
            elif is_mod:
                own = p.author_id == request.user.pk
                author_is_user = author_role < User.ROLE_MODERATOR
                if own or author_is_user:
                    can_del = p.post_order > 1 or topic_post_count == 1
                    deletable_post_ids.update([p.pk] if can_del else [])
                    editable_post_ids.add(p.pk)
            else:
                # Regular user: edit own posts only, no delete
                if p.author_id == request.user.pk:
                    editable_post_ids.add(p.pk)

    # Can current user edit the poll?
    can_edit_poll = False
    if poll is not None and poll.total_votes == 0 and not poll_is_closed:
        if is_admin_view or is_mod or (
            request.user.is_authenticated and topic.author_id == request.user.pk
        ):
            can_edit_poll = True

    liked_post_ids = set()
    if request.user.is_authenticated:
        liked_post_ids = set(
            PostLike.objects.filter(
                user=request.user,
                post__topic=topic,
                post__in=page.object_list,
            ).values_list("post_id", flat=True)
        )
    topic_participants = _get_or_build_topic_participants(topic)

    # Conversion buttons for temporary posts (admin/root only in maintenance/beta)
    convertible_post_ids = set()
    blocked_convert_reasons = {}
    is_admin_or_root = request.user.is_authenticated and (
        request.user.is_root or request.user.role >= User.ROLE_ADMIN
    )
    if is_admin_or_root and _is_temporary_content_mode():
        for p in page.object_list:
            if p.is_temporary:
                ok, reason = can_convert_to_permanent(p)
                if ok:
                    convertible_post_ids.add(p.pk)
                elif reason:
                    blocked_convert_reasons[p.pk] = reason

    # Checklist context
    cl = getattr(topic, "checklist", None) if hasattr(topic, "checklist") else None
    # GeoIP country codes for mods — annotate post objects directly (in-memory, fast)
    if is_mod:
        from .geoip import get_country_code
        for p in page.object_list:
            p.geoip_code = get_country_code(p.author_ip) if p.author_ip else None

    cl_items = []
    cl_pending = []
    cl_upvoted_ids = set()
    cl_categories = []
    cl_is_owner_or_mod = False
    cl_sort = ""
    if cl is not None:
        cl_is_owner_or_mod = request.user.is_authenticated and (
            topic.author_id == request.user.pk
            or request.user.is_root
            or request.user.role >= User.ROLE_ADMIN
        )
        cl_categories = list(cl.categories.all())
        cl_allowed_tags = [t.strip() for t in cl.allowed_tags.split(",") if t.strip()]
        cl_sort = request.GET.get("cl_sort", cl.default_sort)
        items_qs = cl.items.select_related("author", "category")
        # Filtering
        cl_status_filter = request.GET.get("cl_status", "")
        cl_cat_filter = request.GET.get("cl_cat", "")
        cl_tag_filter = request.GET.get("cl_tag", "")
        if cl_status_filter:
            try:
                items_qs = items_qs.filter(status=int(cl_status_filter))
            except (ValueError, TypeError):
                pass
        if cl_cat_filter:
            try:
                items_qs = items_qs.filter(category_id=int(cl_cat_filter))
            except (ValueError, TypeError):
                pass
        if cl_tag_filter and cl_tag_filter in cl_allowed_tags:
            items_qs = items_qs.filter(tag=cl_tag_filter)
        # Exclude PENDING for non-owners (unless it's the user's own)
        if not cl_is_owner_or_mod:
            if request.user.is_authenticated:
                items_qs = items_qs.exclude(
                    ~Q(author=request.user), status=ChecklistItem.Status.PENDING
                )
            else:
                items_qs = items_qs.exclude(status=ChecklistItem.Status.PENDING)
        # Sorting
        if cl_sort == "priority":
            items_qs = items_qs.order_by(django_models.F("priority").asc(nulls_last=True), "order")
        elif cl_sort == "date":
            items_qs = items_qs.order_by("-created_at")
        elif cl_sort == "status":
            items_qs = items_qs.order_by("status", "order")
        else:  # upvotes (default)
            items_qs = items_qs.order_by("-upvote_count", "order")
        all_items = list(items_qs)
        cl_items = [i for i in all_items if i.status != ChecklistItem.Status.PENDING]
        cl_pending = [i for i in all_items if i.status == ChecklistItem.Status.PENDING]
        if request.user.is_authenticated:
            cl_upvoted_ids = set(
                ChecklistUpvote.objects.filter(
                    user=request.user, item__checklist=cl
                ).values_list("item_id", flat=True)
            )

    return render(request, "board/topic_detail.html", {
        "topic": topic,
        "forum": topic.forum,
        "page": page,
        "reply_form": reply_form,
        "visible_post_ids": visible_post_ids,
        "is_moderator": is_mod,
        "dangerous_days": getattr(settings, "IP_BAN_DANGEROUS_DAYS", 90),
        "liked_post_ids": liked_post_ids,
        "poll_is_closed": poll_is_closed,
        "poll_show_results": poll_show_results,
        "poll_can_vote": poll_can_vote,
        "poll_can_change_vote": poll_can_change_vote,
        "poll_user_vote_option_ids": poll_user_vote_option_ids,
        "poll_options_list": poll_options_list if poll else [],
        "poll_max_option_votes": poll_max_option_votes,
        "poll_days_left": poll_days_left,
        "topic_participants": topic_participants,
        "ignored_author_ids": ignored_author_ids,
        "deletable_post_ids": deletable_post_ids,
        "editable_post_ids": editable_post_ids,
        "can_edit_poll": can_edit_poll,
        "convertible_post_ids": convertible_post_ids,
        "blocked_convert_reasons": blocked_convert_reasons,
        "is_admin_or_root": is_admin_or_root,
        "checklist": cl,
        "cl_items": cl_items,
        "cl_pending": cl_pending,
        "cl_upvoted_ids": cl_upvoted_ids,
        "cl_categories": cl_categories,
        "cl_allowed_tags": cl_allowed_tags if cl else [],
        "cl_is_owner_or_mod": cl_is_owner_or_mod,
        "cl_sort": cl_sort,
        "cl_status_filter": cl_status_filter if cl else "",
        "cl_cat_filter": cl_cat_filter if cl else "",
        "cl_tag_filter": cl_tag_filter if cl else "",
    })


@login_required
def vote_poll(request, topic_id):
    if request.method != "POST":
        return redirect("topic_detail", topic_id=topic_id)

    topic = get_object_or_404(Topic.objects.select_related("poll"), pk=topic_id)
    poll = getattr(topic, "poll", None)
    if poll is None or poll.is_archived_import:
        messages.error(request, "W tym wątku nie ma aktywnej ankiety do głosowania.")
        return redirect("topic_detail", topic_id=topic.pk)

    now = timezone.now()
    if poll.is_closed or (poll.ends_at is not None and poll.ends_at <= now):
        messages.error(request, "Ankieta jest już zamknięta.")
        return redirect("topic_detail", topic_id=topic.pk)

    selected_ids_raw = request.POST.getlist("poll_option")
    try:
        selected_ids = [int(value) for value in selected_ids_raw]
    except (TypeError, ValueError):
        selected_ids = []

    if not selected_ids:
        messages.error(request, "Wybierz co najmniej jedną odpowiedź.")
        return redirect("topic_detail", topic_id=topic.pk)

    option_qs = poll.options.filter(pk__in=selected_ids)
    selected_options = list(option_qs)
    if len(selected_options) != len(set(selected_ids)):
        messages.error(request, "Wybrano nieprawidłową odpowiedź ankiety.")
        return redirect("topic_detail", topic_id=topic.pk)

    if not poll.allow_multiple_choice and len(selected_options) != 1:
        messages.error(request, "Ta ankieta pozwala wybrać tylko jedną odpowiedź.")
        return redirect("topic_detail", topic_id=topic.pk)

    if poll.allow_multiple_choice and len(selected_options) > poll.options.count():
        messages.error(request, "Wybrano więcej opcji niż istnieje w ankiecie.")
        return redirect("topic_detail", topic_id=topic.pk)

    existing_votes = list(PollVote.objects.filter(poll=poll, user=request.user))
    if existing_votes and not poll.allow_vote_change:
        messages.error(request, "Swój głos w tej ankiecie można oddać tylko raz.")
        return redirect("topic_detail", topic_id=topic.pk)

    with transaction.atomic():
        if existing_votes:
            PollVote.objects.filter(poll=poll, user=request.user).delete()
        PollVote.objects.bulk_create([
            PollVote(poll=poll, user=request.user, option=option)
            for option in selected_options
        ])

        option_counts = {
            row["option_id"]: row["count"]
            for row in (
                PollVote.objects
                .filter(poll=poll)
                .values("option_id")
                .annotate(count=django_models.Count("id"))
            )
        }
        options_to_update = list(poll.options.all())
        for option in options_to_update:
            option.vote_count = option_counts.get(option.pk, 0)
        PollOption.objects.bulk_update(options_to_update, ["vote_count"])

        total_voters = (
            PollVote.objects.filter(poll=poll)
            .values("user_id")
            .distinct()
            .count()
        )
        Poll.objects.filter(pk=poll.pk).update(total_votes=total_voters)

    messages.success(request, "Głos zapisany.")
    return redirect("topic_detail", topic_id=topic.pk)


@login_required
def toggle_ignore_user(request, user_id):
    if request.method != "POST":
        return redirect(request.POST.get("next") or reverse("index"))

    target = get_object_or_404(User, pk=user_id)
    next_url = request.POST.get("next") or reverse("index")

    if target.pk == request.user.pk:
        messages.error(request, "Nie można ignorować samego siebie.")
        return redirect(next_url)

    ignored = IgnoredUser.objects.filter(owner=request.user, ignored_user=target)
    if ignored.exists():
        ignored.delete()
        messages.success(request, f"Przestałeś ignorować użytkownika {target.username}.")
    else:
        IgnoredUser.objects.create(owner=request.user, ignored_user=target)
        messages.success(request, f"Ignorujesz użytkownika {target.username}.")
    return redirect(next_url)


# ---------------------------------------------------------------------------
# Write views (login required)
# ---------------------------------------------------------------------------

@login_required
def new_topic(request, forum_id):
    """Create a new topic with its first post."""
    if request.user.is_root:
        return HttpResponseForbidden("Konto root nie może tworzyć postów.")
    forum = get_object_or_404(Forum, pk=forum_id)

    is_admin = request.user.role >= User.ROLE_ADMIN
    if request.method == "POST":
        form = NewTopicForm(request.POST, is_admin=is_admin)
        if form.is_valid():
            from .antiflood import check_can_post as _flood_check
            flood = _flood_check(request.user)
            if not flood["allowed"]:
                messages.error(request, str(flood["wait_seconds"]), extra_tags="antiflood")
                return render(request, "board/new_topic.html", {
                    "forum": forum, "form": form, "is_admin": is_admin,
                    "pinned_topic_posts": _get_global_pinned_topic_posts(),
                    "poll_options_text": request.POST.get("poll_options_text", ""),
                    "poll_panel_open": False,
                    "poll_options_soft_limit": SiteConfig.get().poll_options_soft_max,
                    "post_content_soft_limit": getattr(settings, "POST_CONTENT_SOFT_MAX_CHARS", 20_000),
                })
            from .geoip import is_country_blocked
            if is_country_blocked(_get_client_ip(request)):
                messages.error(request, "Tworzenie wątków jest niedostępne z Twojej lokalizacji.")
                return render(request, "board/new_topic.html", {
                    "forum": forum, "form": form, "is_admin": is_admin,
                    "pinned_topic_posts": _get_global_pinned_topic_posts(),
                    "poll_options_text": request.POST.get("poll_options_text", ""),
                    "poll_panel_open": False,
                    "poll_options_soft_limit": SiteConfig.get().poll_options_soft_max,
                    "post_content_soft_limit": getattr(settings, "POST_CONTENT_SOFT_MAX_CHARS", 20_000),
                })
            temp = _is_temporary_content_mode()
            from .moderation_windows import should_hold_for_moderation
            pending = should_hold_for_moderation(request.user)
            topic = Topic.objects.create(
                forum=forum,
                title=form.cleaned_data["title"],
                author=request.user,
                is_temporary=temp,
                is_pending=pending,
            )
            post = _render_and_create_post(
                topic=topic,
                author=request.user,
                content_bbcode=form.cleaned_data["content"],
                post_order=1,
                author_ip=_get_client_ip(request),
                is_temporary=temp,
                is_pending=pending,
            )
            if pending:
                messages.info(
                    request,
                    "Twój wątek trafił do kolejki moderacji i będzie widoczny po zatwierdzeniu.",
                )
                return redirect("forum_detail", forum_id=forum.pk)
            _update_topic_stats(topic, post)
            _update_forum_stats(forum, post)
            _increment_user_post_count(request.user)
            _increment_topic_participant(topic, request.user, post)
            poll_data = form.cleaned_data.get("poll_data")
            if poll_data:
                poll = Poll.objects.create(
                    topic=topic,
                    question=poll_data["question"],
                    ends_at=(
                        timezone.now() + timedelta(days=poll_data["duration_days"])
                        if poll_data["duration_days"] else None
                    ),
                    allow_vote_change=poll_data["allow_vote_change"],
                    allow_multiple_choice=poll_data["allow_multiple_choice"],
                    is_closed=False,
                    is_archived_import=False,
                    total_votes=0,
                )
                PollOption.objects.bulk_create([
                    PollOption(
                        poll=poll,
                        option_text=opt["text"],
                        category=opt["category"],
                        sort_order=index,
                    )
                    for index, opt in enumerate(poll_data["options"], start=1)
                ])
                topic.feature = Topic.Feature.POLL
                topic.save(update_fields=["feature"])

            # Checklist creation (mutually exclusive with poll)
            if not poll_data and request.POST.get("checklist_enabled") == "1":
                cl = Checklist.objects.create(
                    topic=topic,
                    allow_user_proposals=request.POST.get("checklist_allow_proposals") == "1",
                    default_sort=request.POST.get("checklist_default_sort", "upvotes"),
                )
                topic.feature = Topic.Feature.CHECKLIST
                topic.save(update_fields=["feature"])
                # Parse initial categories from textarea
                raw_cats = request.POST.get("checklist_categories", "").strip()
                if raw_cats:
                    cat_names = [ln.strip() for ln in raw_cats.splitlines() if ln.strip()]
                    ChecklistCategory.objects.bulk_create([
                        ChecklistCategory(checklist=cl, name=name, order=idx)
                        for idx, name in enumerate(cat_names)
                    ])

            return redirect("topic_detail", topic_id=topic.pk)
    else:
        form = NewTopicForm(is_admin=is_admin)

    poll_options_text = request.POST.get("poll_options_text", "") if request.method == "POST" else ""
    poll_panel_open = bool(
        request.method == "POST" and (
            request.POST.get("poll_enabled") == "1"
            or (request.POST.get("poll_question") or "").strip()
            or poll_options_text.strip()
            or request.POST.get("poll_duration_days")
            or request.POST.get("poll_allow_vote_change") == "1"
            or request.POST.get("poll_allow_multiple_choice") == "1"
            or form.non_field_errors()
        )
    )

    return render(request, "board/new_topic.html", {
        "forum": forum,
        "form": form,
        "is_admin": is_admin,
        "pinned_topic_posts": _get_global_pinned_topic_posts(),
        "poll_options_text": poll_options_text,
        "poll_panel_open": poll_panel_open,
        "poll_options_soft_limit": SiteConfig.get().poll_options_soft_max,
        "post_content_soft_limit": getattr(settings, "POST_CONTENT_SOFT_MAX_CHARS", 20_000),
    })


@login_required
def reply(request, topic_id):
    """Add a reply post to an existing topic."""
    if request.user.is_root:
        return HttpResponseForbidden("Konto root nie może tworzyć postów.")
    from .user_lock import user_is_locked
    if user_is_locked(request.user):
        messages.error(request, "Twoje konto jest chwilowo zablokowane — trwa operacja systemowa. Spróbuj za chwilę.")
        return redirect("topic_detail", topic_id=topic_id)
    topic = get_object_or_404(Topic, pk=topic_id)

    if topic.is_locked:
        return redirect("topic_detail", topic_id=topic.pk)

    if request.method == "POST":
        form = ReplyForm(request.POST)
        if form.is_valid():
            from .antiflood import check_can_post as _flood_check
            flood = _flood_check(request.user)
            if not flood["allowed"]:
                messages.error(request, str(flood["wait_seconds"]), extra_tags="antiflood")
                return redirect("topic_detail", topic_id=topic_id)
            from .geoip import is_country_blocked
            if is_country_blocked(_get_client_ip(request)):
                messages.error(request, "Pisanie jest niedostępne z Twojej lokalizacji.")
                return redirect("topic_detail", topic_id=topic_id)
            from .moderation_windows import should_hold_for_moderation
            pending = should_hold_for_moderation(request.user)
            next_order = topic.posts.count() + 1
            post = _render_and_create_post(
                topic=topic,
                author=request.user,
                content_bbcode=form.cleaned_data["content"],
                post_order=next_order,
                author_ip=_get_client_ip(request),
                is_temporary=_is_temporary_content_mode(),
                is_pending=pending,
            )
            if pending:
                messages.info(
                    request,
                    "Twój post trafił do kolejki moderacji i będzie widoczny po zatwierdzeniu.",
                )
                return redirect("topic_detail", topic_id=topic.pk)
            _update_topic_stats(topic, post)
            _update_forum_stats(topic.forum, post)
            _increment_user_post_count(request.user)
            _increment_topic_participant(topic, request.user, post)

            # Redirect to the last page so user sees their post
            posts_per_page = getattr(settings, "POSTS_PER_PAGE", 20)
            last_page = (topic.posts.filter(is_pending=False).count() - 1) // posts_per_page + 1
            return redirect(f"/topic/{topic.pk}/?page={last_page}#post-{post.pk}")
    else:
        form = ReplyForm()

    posts_per_page = getattr(settings, "POSTS_PER_PAGE", 20)
    quote_query = (request.GET.get("quote_q") or "").strip()
    quote_author_raw = (request.GET.get("quote_author") or "").strip()
    quote_filter_message = ""
    quote_authors = User.objects.filter(posts__topic=topic).distinct().order_by("username")
    selected_quote_author = None

    recent_posts_qs = (
        topic.posts.select_related("author")
        .order_by("-post_order")
    )

    if quote_author_raw:
        try:
            selected_quote_author = quote_authors.get(pk=int(quote_author_raw))
        except (ValueError, User.DoesNotExist):
            quote_filter_message = "Wybrany autor nie należy do tego wątku."
        else:
            recent_posts_qs = recent_posts_qs.filter(author=selected_quote_author)

    if quote_query and not quote_filter_message:
        parsed_quote = _parse_search_query(quote_query)
        if not parsed_quote["phrases"] and not parsed_quote["term_groups"]:
            if parsed_quote["skipped_terms"]:
                quote_filter_message = (
                    "W filtrze pominięto wyłącznie słowa zbyt częste: "
                    + ", ".join(parsed_quote["skipped_terms"])
                )
                recent_posts_qs = recent_posts_qs.none()
            else:
                quote_filter_message = "Podaj tekst do szukania w wątku."
        else:
            search_rows = PostSearchIndex.objects.filter(topic=topic)
            if selected_quote_author is not None:
                search_rows = search_rows.filter(author=selected_quote_author)
            for phrase in parsed_quote["phrases"]:
                search_rows = search_rows.filter(content_search_author_normalized__contains=phrase)
            for group in parsed_quote["term_groups"]:
                if len(group) == 1:
                    search_rows = search_rows.filter(content_search_author_normalized__contains=group[0])
                else:
                    q = Q()
                    for alt in group:
                        q |= Q(content_search_author_normalized__contains=alt)
                    search_rows = search_rows.filter(q)
            matched_post_ids = [
                row.post_id for row in search_rows.only("post_id", "content_search_author_normalized")
                if _matches_search_text(
                    row.content_search_author_normalized,
                    parsed_quote["phrases"],
                    parsed_quote["term_groups"],
                )
            ]
            recent_posts_qs = recent_posts_qs.filter(pk__in=matched_post_ids)

    recent_posts_page = Paginator(recent_posts_qs, posts_per_page).get_page(
        request.GET.get("quotes_page")
    )
    pinned_topic_posts = _get_global_pinned_topic_posts(exclude_topic=topic)
    return render(request, "board/reply.html", {
        "topic": topic,
        "form": form,
        "recent_posts_page": recent_posts_page,
        "pinned_topic_posts": pinned_topic_posts,
        "quote_authors": quote_authors,
        "quote_query": quote_query,
        "selected_quote_author": selected_quote_author,
        "quote_filter_message": quote_filter_message,
        "post_content_soft_limit": getattr(settings, "POST_CONTENT_SOFT_MAX_CHARS", 20_000),
    })


@login_required
def delete_post(request, post_id):
    """Delete a post. Permissions: admin=all, moderator=own+users, user=none."""
    if request.method != "POST":
        return redirect("index")

    post = get_object_or_404(Post.objects.select_related("author", "topic__forum"), pk=post_id)
    topic = post.topic
    forum = topic.forum
    user = request.user

    is_admin = user.role >= User.ROLE_ADMIN
    is_mod = _is_moderator(user, forum)
    author_role = post.author.role if post.author else User.ROLE_USER

    if is_admin:
        can_delete = True
    elif is_mod:
        # Moderator can delete own posts and posts by regular users only
        own_post = post.author_id == user.pk
        author_is_user = author_role < User.ROLE_MODERATOR
        can_delete = own_post or author_is_user
    else:
        can_delete = False

    if not can_delete:
        messages.error(request, "Nie masz uprawnień do usunięcia tego postu.")
        return redirect("topic_detail", topic_id=topic.pk)

    with transaction.atomic():
        remaining = topic.posts.count()

        if remaining == 1:
            # Last post → delete whole topic (with poll etc.)
            if post.author and not post.is_pending:
                from .active_days import decrement_if_last_on_day
                decrement_if_last_on_day(post.author, post)
            forum_id = forum.pk
            topic.delete()
            messages.success(request, "Usunięto ostatni post — wątek został usunięty.")
            forum.refresh_from_db()
            forum.post_count = Post.objects.filter(topic__forum=forum, is_pending=False).count()
            forum.topic_count = forum.topics.filter(is_pending=False).count()
            new_last = Post.objects.filter(topic__forum=forum, is_pending=False).order_by("-created_at").first()
            forum.last_post = new_last
            forum.last_post_at = new_last.created_at if new_last else None
            forum.save(update_fields=["post_count", "topic_count", "last_post", "last_post_at"])
            return redirect("forum_detail", forum_id=forum_id)

        if post.post_order == 1:
            # First post with siblings — cannot delete, only hide
            messages.error(request, "Pierwszego postu nie można usunąć gdy wątek ma więcej postów. Można go tylko ukryć.")
            return redirect("topic_detail", topic_id=topic.pk)

        # Delete the post and renumber remaining posts
        deleted_order = post.post_order
        if post.author and not post.is_pending:
            from .active_days import decrement_if_last_on_day
            decrement_if_last_on_day(post.author, post)
            post.author.post_count = max(0, post.author.post_count - 1)
            post.author.save(update_fields=["post_count"])
        post.delete()
        topic.posts.filter(post_order__gt=deleted_order).update(
            post_order=django_models.F("post_order") - 1
        )
        # Update topic stats (exclude pending posts from counts)
        new_last_post = topic.posts.filter(is_pending=False).order_by("-created_at").first()
        if new_last_post:
            topic.reply_count = topic.posts.filter(is_pending=False).count() - 1
            topic.last_post = new_last_post
            topic.last_post_at = new_last_post.created_at
            topic.save(update_fields=["reply_count", "last_post", "last_post_at"])
        # Update forum stats
        forum.post_count = Post.objects.filter(topic__forum=forum, is_pending=False).count()
        new_forum_last = Post.objects.filter(topic__forum=forum, is_pending=False).order_by("-created_at").first()
        forum.last_post = new_forum_last
        forum.last_post_at = new_forum_last.created_at if new_forum_last else None
        forum.save(update_fields=["post_count", "last_post", "last_post_at"])
        messages.success(request, "Post usunięty.")

    return redirect("topic_detail", topic_id=topic.pk)


@login_required
def edit_post(request, post_id):
    post = get_object_or_404(Post.objects.select_related("author", "topic__forum"), pk=post_id)
    topic = post.topic
    forum = topic.forum
    user = request.user

    from .user_lock import user_is_locked
    # Block edit if the requesting user or the post's author is being processed
    if user_is_locked(user) or (post.author and user_is_locked(post.author)):
        messages.error(request, "Edycja chwilowo niedostępna — trwa operacja systemowa. Spróbuj za chwilę.")
        return redirect("topic_detail", topic_id=topic.pk)

    is_admin = user.role >= User.ROLE_ADMIN
    is_mod = _is_moderator(user, forum)
    author_role = post.author.role if post.author else User.ROLE_USER

    if is_admin:
        can_edit = True
    elif is_mod:
        own = post.author_id == user.pk
        author_is_user = author_role < User.ROLE_MODERATOR
        can_edit = own or author_is_user
    else:
        can_edit = post.author_id == user.pk

    if not can_edit:
        messages.error(request, "Nie masz uprawnień do edycji tego postu.")
        return redirect("topic_detail", topic_id=topic.pk)

    original_size = len(post.content_bbcode)
    if request.method == "POST":
        form = ReplyForm(request.POST, original_size=original_size)
        if form.is_valid():
            post.content_bbcode = form.cleaned_data["content"]
            post.edit_count += 1
            post.updated_by = user
            post.updated_at = timezone.now()
            post.save(update_fields=["content_bbcode", "edit_count", "updated_by", "updated_at"])
            messages.success(request, "Post zaktualizowany.")
            return redirect(f"{reverse('topic_detail', args=[topic.pk])}#post-{post.pk}")
    else:
        form = ReplyForm(initial={"content": post.content_bbcode}, original_size=original_size)

    return render(request, "board/edit_post.html", {
        "form": form,
        "post": post,
        "topic": topic,
        "post_content_soft_limit": getattr(settings, "POST_CONTENT_SOFT_MAX_CHARS", 20_000),
    })


@login_required
def edit_poll(request, topic_id):
    topic = get_object_or_404(Topic.objects.select_related("forum"), pk=topic_id)
    forum = topic.forum
    poll = getattr(topic, "poll", None)
    user = request.user

    if poll is None:
        messages.error(request, "Ten wątek nie ma ankiety.")
        return redirect("topic_detail", topic_id=topic.pk)

    if poll.total_votes > 0:
        messages.error(request, "Nie można edytować ankiety, gdy oddano już głosy.")
        return redirect("topic_detail", topic_id=topic.pk)

    is_admin = user.role >= User.ROLE_ADMIN
    is_mod = _is_moderator(user, forum)
    is_author = topic.author_id == user.pk

    if not (is_admin or is_mod or is_author):
        messages.error(request, "Nie masz uprawnień do edycji tej ankiety.")
        return redirect("topic_detail", topic_id=topic.pk)

    from .forms import parse_poll_options_text, poll_options_to_text
    from .polls import validate_poll_option_count

    if request.method == "POST":
        question = request.POST.get("poll_question", "").strip()
        allow_multiple = request.POST.get("allow_multiple_choice") == "1"
        duration_raw = request.POST.get("poll_duration_days", "").strip()
        raw_text = request.POST.get("poll_options_text", "")

        new_options, option_errors = parse_poll_options_text(raw_text)

        errors = []
        if not question:
            errors.append("Pytanie nie może być puste.")
        errors.extend(option_errors)
        if not option_errors and len(new_options) < 2:
            errors.append("Ankieta musi mieć co najmniej 2 opcje.")
        _, limit_errors = validate_poll_option_count(len(new_options))
        errors.extend(limit_errors)

        duration_days = None
        if duration_raw:
            try:
                duration_days = int(duration_raw)
                if duration_days < 1:
                    raise ValueError
            except ValueError:
                errors.append("Czas trwania musi być liczbą całkowitą ≥ 1.")
        elif not is_admin:
            errors.append("Podaj czas trwania (tylko admin może zostawić puste = bezterminowo).")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            with transaction.atomic():
                poll.question = question
                poll.allow_multiple_choice = allow_multiple
                poll.ends_at = (
                    timezone.now() + timedelta(days=duration_days)
                    if duration_days else None
                )
                poll.save(update_fields=["question", "allow_multiple_choice", "ends_at"])
                poll.options.all().delete()
                PollOption.objects.bulk_create([
                    PollOption(
                        poll=poll,
                        option_text=opt["text"],
                        category=opt["category"],
                        sort_order=idx,
                    )
                    for idx, opt in enumerate(new_options, start=1)
                ])
            messages.success(request, "Ankieta zaktualizowana.")
            return redirect("topic_detail", topic_id=topic.pk)

        poll_options_text = raw_text  # preserve on error
    else:
        poll_options_text = poll_options_to_text(poll.options.all())

    # Current duration in days (approx)
    current_days = None
    if poll.ends_at:
        delta = poll.ends_at - timezone.now()
        current_days = max(1, math.ceil(delta.total_seconds() / 86400))

    return render(request, "board/edit_poll.html", {
        "poll": poll,
        "topic": topic,
        "poll_options_text": poll_options_text,
        "current_days": current_days,
        "is_admin": is_admin,
    })


def preview_post(request, topic_id):
    """AJAX: validate and render BBCode text with the same rules as form submit."""
    from django.http import JsonResponse
    from .bbcode import render as bbcode_render
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    text = request.POST.get("content", "")
    repaired, changes, errors = validate_post_content(text)
    if errors:
        return JsonResponse({
            "ok": False,
            "errors": errors,
            "changes": changes,
        })
    html = bbcode_render(repaired)
    return JsonResponse({
        "ok": True,
        "html": html,
        "content": repaired,
        "changes": changes,
    })


@login_required
def preview_new_topic(request, forum_id):
    """AJAX: validate and render BBCode text for the new-topic editor.

    Also validates poll options (if poll_enabled=1) and returns poll preview HTML.
    """
    from django.http import JsonResponse
    from django.utils.html import escape
    from .bbcode import render as bbcode_render
    from .forms import parse_poll_options_text

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    get_object_or_404(Forum, pk=forum_id)
    text = request.POST.get("content", "")
    repaired, changes, errors = validate_post_content(text)

    # Poll validation
    poll_enabled = request.POST.get("poll_enabled") == "1"
    poll_html = ""
    if poll_enabled:
        poll_question = request.POST.get("poll_question", "").strip()
        poll_options_raw = request.POST.get("poll_options_text", "").strip()
        if not poll_question:
            errors.append("Pytanie ankiety jest wymagane.")
        if not poll_options_raw:
            errors.append("Ankieta musi mieć przynajmniej jedną opcję.")
        else:
            options, poll_errors = parse_poll_options_text(poll_options_raw)
            errors.extend(poll_errors)
            if not options and not poll_errors:
                errors.append("Ankieta musi mieć przynajmniej jedną opcję.")
            if not errors:
                # Build poll preview HTML (voting format — shows categories clearly)
                poll_html = _render_poll_preview(escape(poll_question), options)

    if errors:
        return JsonResponse({
            "ok": False,
            "errors": errors,
            "changes": changes,
        })
    return JsonResponse({
        "ok": True,
        "html": bbcode_render(repaired),
        "content": repaired,
        "changes": changes,
        "poll_html": poll_html,
    })


def _render_poll_preview(question_escaped, options):
    """Render poll preview HTML in voting format (categories visible)."""
    from django.utils.html import escape
    lines = []
    lines.append(f'<div style="border:1px solid #BC8F8F;padding:.55rem .7rem;margin-top:.5rem;background:#fffcfc;">')
    lines.append(f'<div style="font-weight:bold;margin-bottom:.45rem;">{question_escaped}</div>')
    current_cat = None
    for opt in options:
        if opt["category"] != current_cat:
            current_cat = opt["category"]
            if current_cat:
                lines.append(
                    f'<div style="font-size:11px;font-weight:bold;color:#5a2020;margin-top:.4rem;'
                    f'border-bottom:1px solid #d7b2b2;padding-bottom:.1rem;">'
                    f'{escape(current_cat)}</div>'
                )
        lines.append(
            f'<label style="display:flex;gap:.45rem;align-items:flex-start;">'
            f'<input type="radio" disabled>'
            f'<span>{escape(opt["text"])}</span></label>'
        )
    lines.append(f'<div style="font-size:11px;color:#555;margin-top:.4rem;">Podgląd ankiety — {len(options)} opcji</div>')
    lines.append('</div>')
    return "\n".join(lines)


@login_required
def quote_fragment(request, post_id):
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)

    post = get_object_or_404(Post.objects.select_related("author").only("pk", "content_bbcode", "author_id"), pk=post_id)
    from .user_lock import user_is_locked
    if post.author and user_is_locked(post.author):
        return JsonResponse({"ok": False, "error": "Cytowanie chwilowo niedostępne — trwa operacja systemowa."}, status=503)
    selected_text = normalize_selected_text(request.POST.get("selected_text", ""))
    fragment = extract_exact_quote_fragment(post.content_bbcode or "", selected_text)

    return JsonResponse({
        "ok": True,
        "body": fragment or selected_text,
        "exact_source": bool(fragment),
    })


def contact(request):
    """Contact form — open to everyone, sends to admin email (CONTACT_FORM_RECIPIENT).

    Rate limit: max CONTACT_FORM_RATE_LIMIT messages per IP per hour (tracked in session).
    User provides their email in plaintext — the only place in the system where
    a non-admin email is sent as plaintext (and only to the admin, not stored in DB).
    """
    recipient = getattr(settings, "CONTACT_FORM_RECIPIENT", "")
    rate_limit = getattr(settings, "CONTACT_FORM_RATE_LIMIT", 3)

    sent = False
    error = None

    if request.method == "POST":
        # Rate limiting via session
        from django.utils import timezone as tz
        import datetime
        now = tz.now()
        window_start = request.session.get("contact_window_start")
        count = request.session.get("contact_count", 0)

        if window_start:
            window_start = datetime.datetime.fromisoformat(window_start)
            if (now - window_start).total_seconds() > 3600:
                count = 0
                window_start = None

        if count >= rate_limit:
            error = f"Wysłałeś zbyt wiele wiadomości. Spróbuj ponownie za godzinę."
        else:
            sender_email = request.POST.get("email", "").strip()
            message = request.POST.get("message", "").strip()

            if not sender_email or not message:
                error = "Wypełnij oba pola."
            elif not recipient:
                error = "Formularz kontaktowy nie jest skonfigurowany. Skontaktuj się bezpośrednio z administratorem."
            else:
                send_mail(
                    subject=f"[Forum] Wiadomość od {sender_email}",
                    message=f"Od: {sender_email}\n\n{message}",
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum"),
                    recipient_list=[recipient],
                    fail_silently=False,
                )
                request.session["contact_count"] = count + 1
                request.session["contact_window_start"] = (window_start or now).isoformat()
                sent = True

    return render(request, "board/contact.html", {"sent": sent, "error": error})


_SEARCH_PHRASE_RE = re.compile(r'"([^"]+)"|(\S+)')
_SAFE_STOP_WORDS = {
    "nie", "to", "w", "i", "sie", "ze", "na", "z", "a", "do", "o", "ale",
}
_SEARCH_BOUNDARY = r"(?<!\w){needle}(?!\w)"


def _parse_search_query(raw_query: str):
    # Spacje wokół | są ignorowane: "pies+| kot" → "pies+|kot"
    raw_query = re.sub(r'\s*\|\s*', '|', raw_query or '')
    phrases = []
    term_groups = []
    skipped_terms = []
    display_parts = []   # do zbudowania expanded_query
    has_expansion = False

    for match in _SEARCH_PHRASE_RE.finditer(raw_query or ""):
        phrase = match.group(1)
        token = match.group(2)
        if phrase is not None:
            normalized_phrase = normalize_search_text(phrase)
            if normalized_phrase:
                phrases.append(normalized_phrase)
                display_parts.append(f'"{phrase}"')
            continue

        alternatives = (token or "").split("|")
        group = []
        group_skipped = []
        display_alts = []

        for alt in alternatives:
            do_expand_all = alt.endswith("++")
            do_expand     = not do_expand_all and alt.endswith("+")
            if do_expand_all:
                base = alt[:-2]
            elif do_expand:
                base = alt[:-1]
            else:
                base = alt
            normalized = normalize_search_text(base)
            if not normalized:
                continue
            if normalized in _SAFE_STOP_WORDS:
                group_skipped.append(base)
                continue
            if do_expand_all:
                expanded = expand_morph_term_all(normalized)
                group.extend(expanded)
                display_alts.extend(expanded)
                has_expansion = True
            elif do_expand:
                expanded = expand_morph_term(normalized)
                group.extend(expanded)
                display_alts.extend(expanded)
                has_expansion = True
            else:
                group.append(normalized)
                display_alts.append(normalized)

        if group:
            # dedupl z zachowaniem kolejności (obejmuje ręczne alt + ekspansje)
            seen_g: set[str] = set()
            deduped = [x for x in group if not (x in seen_g or seen_g.add(x))]
            term_groups.append(deduped)
            display_parts.append("| ".join(deduped))
        else:
            skipped_terms.extend(group_skipped)

    expanded_query = " ".join(display_parts) if has_expansion else ""

    return {
        "phrases": phrases,
        "term_groups": term_groups,
        "skipped_terms": skipped_terms,
        "expanded_query": expanded_query,
        "has_expansion": has_expansion,
    }


def _build_search_pattern(needle: str):
    return re.compile(
        _SEARCH_BOUNDARY.format(needle=re.escape(needle)),
        re.IGNORECASE,
    )


def _find_match_start(haystack: str, needle: str):
    match = _build_search_pattern(needle).search(haystack or "")
    return match.start() if match else -1


def _matches_search_text(text_norm: str, phrases: list[str], term_groups: list[list[str]]) -> bool:
    for phrase in phrases:
        if _find_match_start(text_norm, phrase) == -1:
            return False
    for group in term_groups:
        if not any(_find_match_start(text_norm, term) != -1 for term in group):
            return False
    return True


def _normalize_for_match(text: str) -> str:
    return strip_diacritics((text or "").lower())


def _find_highlight_spans(snippet: str, needles: list[str]):
    if not snippet:
        return []

    normalized_chars = []
    original_spans = []
    for idx, char in enumerate(snippet):
        normalized = strip_diacritics(char.lower())
        if not normalized:
            continue
        for item in normalized:
            normalized_chars.append(item)
            original_spans.append((idx, idx + 1))

    normalized_snippet = "".join(normalized_chars)
    spans = []
    seen = set()
    for needle in sorted({n for n in needles if n}, key=len, reverse=True):
        for match in _build_search_pattern(needle).finditer(normalized_snippet):
            start_norm, end_norm = match.span()
            start_orig = original_spans[start_norm][0]
            end_orig = original_spans[end_norm - 1][1]
            if (start_orig, end_orig) in seen:
                continue
            seen.add((start_orig, end_orig))
            spans.append((start_orig, end_orig))

    spans.sort()
    merged = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
            continue
        merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _highlight_snippet(snippet: str, needles: list[str]) -> str:
    spans = _find_highlight_spans(snippet, needles)
    if not spans:
        return escape(snippet)

    parts = []
    pos = 0
    for start, end in spans:
        if start > pos:
            parts.append(escape(snippet[pos:start]))
        parts.append(
            '<span style="background:#d96a00;color:#fff;padding:0 .15rem;'
            'border-radius:2px;font-weight:bold;">'
            f'{escape(snippet[start:end])}</span>'
        )
        pos = end
    if pos < len(snippet):
        parts.append(escape(snippet[pos:]))
    return "".join(parts)


def _build_search_snippet(text: str, phrases: list[str], term_groups: list[list[str]], df_map: dict[str, int], width: int = 220):
    text = (text or "").strip()
    if not text:
        return ""

    text_norm = _normalize_for_match(text)
    anchor = None
    matched_needles = []
    terms = [term for group in term_groups for term in group]

    for phrase in phrases:
        idx = _find_match_start(text_norm, phrase)
        if idx != -1:
            anchor = (idx, phrase)
            matched_needles.append(phrase)
            break

    if anchor is None:
        present_terms = []
        for term in terms:
            idx = _find_match_start(text_norm, term)
            if idx != -1:
                present_terms.append((df_map.get(term, 10**9), idx, term))
        if present_terms:
            present_terms.sort(key=lambda item: (item[0], item[1], item[2]))
            _, idx, chosen = present_terms[0]
            anchor = (idx, chosen)
            matched_needles.append(chosen)

    for term in terms:
        if term != (anchor[1] if anchor else None) and _find_match_start(text_norm, term) != -1:
            matched_needles.append(term)
    for phrase in phrases:
        if phrase != (anchor[1] if anchor else None) and _find_match_start(text_norm, phrase) != -1:
            matched_needles.append(phrase)

    if anchor is None:
        snippet = text[:width]
        if len(text) > width:
            snippet = snippet.rstrip() + "..."
        return _highlight_snippet(snippet, matched_needles)

    pos = anchor[0]
    start = max(0, pos - width // 2)
    end = min(len(text), start + width)
    start = max(0, end - width)

    while start > 0 and text[start] not in " \n\t":
        start -= 1
    while end < len(text) and text[end - 1] not in " \n\t":
        end += 1
        if end >= len(text):
            end = len(text)
            break

    snippet = text[start:end].strip()
    if start > 0:
        snippet = "... " + snippet
    if end < len(text):
        snippet = snippet + " ..."
    return _highlight_snippet(snippet, matched_needles)


def _build_plain_post_snippet(text: str, width: int = 320) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    if len(text) <= width:
        return escape(text)
    cut = text[:width]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return escape(cut.rstrip() + " ...")


def _parse_search_bound(value: str, *, is_end: bool):
    value = (value or "").strip()
    if not value:
        return None

    dt = parse_datetime(value)
    if dt is not None:
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt

    only_date = parse_date(value)
    if only_date is None:
        return None

    bound_time = time.max if is_end else time.min
    return timezone.make_aware(
        datetime.combine(only_date, bound_time),
        timezone.get_current_timezone(),
    )


@login_required
def search(request):
    raw_query = (request.GET.get("q") or "").strip()
    forum_id_raw = (request.GET.get("forum_id") or "").strip()
    author_query_raw = (request.GET.get("author") or "").strip()
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()
    search_mode = (request.GET.get("mode") or "posts").strip().lower()
    if search_mode not in {"posts", "topics"}:
        search_mode = "posts"
    search_filter = (request.GET.get("kind") or "all").strip().lower()
    allowed_filters = {
        "posts": {"all", "links", "youtube", "liked"},
        "topics": {"all", "polls"},
    }
    if search_filter not in allowed_filters[search_mode]:
        search_filter = "all"
    page_num = request.GET.get("page")

    indexed_forums = Forum.objects.filter(search_posts__isnull=False).distinct().order_by("title")
    selected_forum = None
    selected_author = None
    parsed = {"phrases": [], "term_groups": [], "skipped_terms": [], "expanded_query": "", "has_expansion": False}
    page = None
    info_message = ""
    snippet_width = max(80, getattr(settings, "SEARCH_SNIPPET_CHARS", 800))
    date_from = None
    date_to = None

    try:
        snippet_width = max(80, SiteConfig.get().search_snippet_chars)
    except Exception:
        pass

    pagination_query = request.GET.copy()
    pagination_query.pop("page", None)
    page_query = pagination_query.urlencode()

    if forum_id_raw:
        try:
            selected_forum = indexed_forums.get(pk=int(forum_id_raw))
        except (ValueError, Forum.DoesNotExist):
            info_message = "Wybrane forum nie istnieje w indeksie wyszukiwania."

    if author_query_raw and not info_message:
        selected_author = User.objects.filter(
            username_normalized=normalize(author_query_raw)
        ).first()
        if selected_author is None:
            info_message = "Nie znaleziono użytkownika o podanym nicku."

    if not info_message and date_from_raw:
        date_from = _parse_search_bound(date_from_raw, is_end=False)
        if date_from is None:
            info_message = "Nieprawidłowa data początkowa."

    if not info_message and date_to_raw:
        date_to = _parse_search_bound(date_to_raw, is_end=True)
        if date_to is None:
            info_message = "Nieprawidłowa data końcowa."

    if not info_message and date_from and date_to and date_from > date_to:
        info_message = "Data początkowa nie może być późniejsza niż końcowa."

    if (raw_query or search_filter != "all" or selected_author is not None or date_from or date_to) and not info_message:
        parsed = _parse_search_query(raw_query)
        if not parsed["phrases"] and not parsed["term_groups"]:
            if search_filter != "all" or selected_author is not None or date_from or date_to:
                pass
            elif parsed["skipped_terms"]:
                info_message = (
                    "Zapytanie składa się wyłącznie ze słów pomijanych: "
                    + ", ".join(parsed["skipped_terms"])
                )
            else:
                info_message = "Podaj szukany tekst."
        if not info_message:
            max_forum_level = getattr(request.user, "archive_access", 0)
            if search_mode == "topics":
                ts_field, ignore_q = get_topic_visibility_filter(request.user)
                qs = (
                    Topic.objects
                    .select_related("forum", "author", "last_post", "last_post__author", "poll")
                    .filter(forum__archive_level__lte=max_forum_level)
                    .filter(**{f"{ts_field}__isnull": False})
                    .exclude(ignore_q)
                )
                if selected_forum is not None:
                    qs = qs.filter(forum=selected_forum)
                if selected_author is not None:
                    qs = qs.filter(author=selected_author)
                if date_from is not None:
                    qs = qs.filter(created_at__gte=date_from)
                if date_to is not None:
                    qs = qs.filter(created_at__lte=date_to)
                if search_filter == "polls":
                    qs = qs.filter(poll__isnull=False)

                matched_topics = []
                for topic in qs.order_by("-created_at", "-pk"):
                    title_normalized = normalize_search_text(topic.title)
                    if parsed["phrases"] or parsed["term_groups"]:
                        if not _matches_search_text(
                            title_normalized,
                            parsed["phrases"],
                            parsed["term_groups"],
                        ):
                            continue
                    topic.title_html = _highlight_snippet(
                        topic.title,
                        parsed["phrases"] + [t for g in parsed["term_groups"] for t in g],
                    )
                    topic.has_poll = getattr(topic, "poll", None) is not None
                    matched_topics.append(topic)

                _annotate_topics_with_unread_state(
                    request.user,
                    matched_topics,
                    getattr(settings, "POSTS_PER_PAGE", 20),
                )
                paginator = Paginator(matched_topics, getattr(settings, "TOPICS_PER_PAGE", 30))
                page = paginator.get_page(page_num)
            else:
                qs = (
                    PostSearchIndex.objects
                    .select_related("post", "author", "topic", "forum")
                    .filter(forum__archive_level__lte=max_forum_level)
                    .filter(get_author_spam_filter(request.user))
                )
                if selected_forum is not None:
                    qs = qs.filter(forum=selected_forum)
                if selected_author is not None:
                    qs = qs.filter(author=selected_author)
                if date_from is not None:
                    qs = qs.filter(created_at__gte=date_from)
                if date_to is not None:
                    qs = qs.filter(created_at__lte=date_to)
                if search_filter == "links":
                    qs = qs.filter(has_link=True)
                elif search_filter == "youtube":
                    qs = qs.filter(has_youtube=True)
                elif search_filter == "liked" and request.user.is_authenticated:
                    qs = qs.filter(post__likes__user=request.user)

                for phrase in parsed["phrases"]:
                    qs = qs.filter(content_search_author_normalized__contains=phrase)
                for group in parsed["term_groups"]:
                    if len(group) == 1:
                        qs = qs.filter(content_search_author_normalized__contains=group[0])
                    else:
                        q = Q()
                        for alt in group:
                            q |= Q(content_search_author_normalized__contains=alt)
                        qs = qs.filter(q)

                matched_rows = [
                    row for row in qs.order_by("-created_at", "-post_id")
                    if _matches_search_text(
                        row.content_search_author_normalized,
                        parsed["phrases"],
                        parsed["term_groups"],
                    )
                ]

                paginator = Paginator(matched_rows, getattr(settings, "POSTS_PER_PAGE", 20))
                page = paginator.get_page(page_num)
                if page is not None:
                    flat_terms = [term for group in parsed["term_groups"] for term in group]
                    df_map = {}
                    for term in flat_terms:
                        df_map[term] = sum(
                            1 for row in matched_rows
                            if _find_match_start(row.content_search_author_normalized, term) != -1
                        )
                    for row in page.object_list:
                        row.snippet_html = _build_search_snippet(
                            row.content_search_author,
                            parsed["phrases"],
                            parsed["term_groups"],
                            df_map,
                            width=snippet_width,
                        )

    return render(request, "board/search.html", {
        "indexed_forums": indexed_forums,
        "selected_forum": selected_forum,
        "selected_author": selected_author,
        "author_query": author_query_raw,
        "date_from_raw": date_from_raw,
        "date_to_raw": date_to_raw,
        "raw_query": raw_query,
        "parsed_query": parsed,
        "info_message": info_message,
        "page": page,
        "search_mode": search_mode,
        "search_filter": search_filter,
        "page_query": page_query,
    })


def new_posts(request):
    max_forum_level = getattr(request.user, "archive_access", 0) if request.user.is_authenticated else 0
    posts = (
        Post.objects.select_related("author", "topic", "topic__forum")
        .filter(topic__forum__archive_level__lte=max_forum_level)
        .filter(get_author_spam_filter(request.user))
        .order_by("-created_at", "-pk")
    )
    page = Paginator(posts, getattr(settings, "POSTS_PER_PAGE", 20)).get_page(request.GET.get("page"))

    for post in page.object_list:
        post.snippet_html = _build_plain_post_snippet(
            extract_author_search_text(post.content_bbcode),
            width=min(500, max(180, getattr(settings, "SEARCH_SNIPPET_CHARS", 800))),
        )

    return render(request, "board/new_posts.html", {
        "page": page,
    })


def new_topics(request):
    max_forum_level = getattr(request.user, "archive_access", 0) if request.user.is_authenticated else 0
    ts_field, ignore_q = get_topic_visibility_filter(request.user)
    topics = (
        Topic.objects.select_related("author", "forum", "last_post", "last_post__author")
        .filter(forum__archive_level__lte=max_forum_level)
        .filter(**{f"{ts_field}__isnull": False})
        .exclude(ignore_q)
        .order_by("-created_at", "-pk")
    )
    page = Paginator(topics, getattr(settings, "TOPICS_PER_PAGE", 30)).get_page(request.GET.get("page"))
    _annotate_topics_with_unread_state(
        request.user,
        page.object_list,
        getattr(settings, "POSTS_PER_PAGE", 20),
    )

    return render(request, "board/new_topics.html", {
        "page": page,
    })


@login_required
def my_topics(request):
    participations = (
        TopicParticipant.objects.select_related(
            "topic",
            "topic__forum",
            "topic__author",
            "topic__last_post",
            "topic__last_post__author",
        )
        .filter(
            user=request.user,
            topic__forum__archive_level__lte=request.user.archive_access,
        )
        .order_by("-last_post_at", "-topic_id")
    )
    page = Paginator(participations, getattr(settings, "TOPICS_PER_PAGE", 30)).get_page(
        request.GET.get("page")
    )
    _annotate_topics_with_unread_state(
        request.user,
        [row.topic for row in page.object_list],
        getattr(settings, "POSTS_PER_PAGE", 20),
    )

    return render(request, "board/my_topics.html", {
        "page": page,
    })


def user_profile(request, user_id):
    profile = get_object_or_404(User, pk=user_id)
    return render(request, "board/user_profile.html", {"profile": profile})


def user_list(request):
    q = (request.GET.get("q") or "").strip()
    users = User.objects.order_by("-post_count", "username")
    if q:
        users = users.filter(username__icontains=q)
    page = Paginator(users, 50).get_page(request.GET.get("page"))
    return render(request, "board/user_list.html", {"page": page, "q": q})


def unanswered_topics(request):
    max_forum_level = getattr(request.user, "archive_access", 0) if request.user.is_authenticated else 0
    topics = (
        Topic.objects.select_related("author", "forum", "last_post", "last_post__author")
        .filter(forum__archive_level__lte=max_forum_level, reply_count=0)
        .order_by("-created_at", "-pk")
    )
    page = Paginator(topics, getattr(settings, "TOPICS_PER_PAGE", 30)).get_page(request.GET.get("page"))

    return render(request, "board/unanswered_topics.html", {
        "page": page,
    })


@login_required
def unread_topics(request):
    max_forum_level = getattr(request.user, "archive_access", 0)
    posts_per_page = getattr(settings, "POSTS_PER_PAGE", 20)
    ts_field, ignore_q = get_topic_visibility_filter(request.user)
    topics = list(
        Topic.objects.select_related("author", "forum", "last_post", "last_post__author")
        .filter(forum__archive_level__lte=max_forum_level)
        .exclude(last_post__isnull=True)
        .filter(**{f"{ts_field}__gt": request.user.mark_all_read_at})
        .exclude(ignore_q)
        .order_by("-last_post_at", "-pk")
    )
    read_state_map = {
        state.topic_id: state
        for state in TopicReadState.objects.filter(
            user=request.user,
            topic_id__in=[topic.pk for topic in topics],
        )
    }
    unread = []
    baseline = request.user.mark_all_read_at
    for topic in topics:
        state = read_state_map.get(topic.pk)
        last_post = topic.last_post
        if last_post is None:
            continue
        if state is not None:
            if state.last_read_post_order >= last_post.post_order:
                continue
        elif topic.last_post_at and topic.last_post_at <= baseline:
            continue
        topic.has_unread = True
        topic.unread_url = _build_unread_topic_url(request.user, topic, state, posts_per_page)
        unread.append(topic)

    unread_limit = getattr(settings, "UNREAD_TOPICS_MAX", 500)
    truncated = len(unread) > unread_limit
    if truncated:
        unread = unread[:unread_limit]

    page = Paginator(unread, getattr(settings, "TOPICS_PER_PAGE", 30)).get_page(request.GET.get("page"))
    return render(request, "board/unread_topics.html", {
        "page": page,
        "truncated": truncated,
        "unread_limit": unread_limit,
    })


@login_required
def mark_all_topics_read(request):
    if request.method != "POST":
        return redirect("unread_topics")

    request.user.mark_all_read_at = timezone.now()
    request.user.save(update_fields=["mark_all_read_at"])
    TopicReadState.objects.filter(user=request.user).delete()
    messages.success(request, "Wszystkie wątki oznaczono jako przeczytane.")
    return redirect("unread_topics")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
# TODO: Client-side Argon2 + server-side SHA3-256 for password login
#
# Flow:
#   1. Client requests salt for given username (server returns salt, reveals
#      whether username exists — accepted trade-off, see design notes)
#   2. Client computes Argon2(password, salt) in JS (argon2-browser library)
#   3. Client sends Argon2 result to server
#   4. Server computes SHA3-256(received) and compares with stored hash
#
# Benefit: Argon2 work factor runs on client, not server — DDoS-resistant.
# Stored value: SHA3-256(Argon2(pw, salt)) — replay attack with leaked DB
# requires inverting SHA3, which is infeasible.
#
# Email hash stays server-side Argon2 (entered during password reset, no JS).
# ---------------------------------------------------------------------------

def register(request):
    """User registration — 5-case logic based on nick×email availability.

    Cases (for real accounts):
      1.1  nick free + email free  → new account
      1.2  nick taken + email free → error "nick zajęty"
      1.3  email taken + nick free → recover: send code+nick to email
      1.4  nick taken + email taken, SAME user → "konto istnieje, zaloguj się"
      1.5  nick taken + email taken, DIFFERENT users → same as 1.3

    Temporary accounts (maintenance/beta): only check nick uniqueness.
    """
    from .models import SiteConfig
    if request.user.is_authenticated:
        return redirect("/")

    cfg = SiteConfig.get()
    is_temp_mode = cfg.site_mode in (SiteConfig.MODE_MAINTENANCE, SiteConfig.MODE_BETA)

    pending = request.session.get("register_pending")
    reg_type = pending.get("reg_type") if pending else None
    start_form = RegisterStartForm(initial=pending or None)
    finish_form = RegisterFinishForm()
    sent = False
    test_code = None
    test_nick = None  # shown in TEST_MODE for recover flow
    error = None

    def clear_pending_registration():
        for key in (
            "register_pending",
            "register_code",
            "register_code_sent_at",
            "register_code_expires_at",
            "register_code_attempts",
        ):
            request.session.pop(key, None)

    def _generate_code():
        """Generate a 6-digit code and store in session."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = timezone.now()
        expires = now + timedelta(hours=4)
        request.session["register_code"] = code
        request.session["register_code_sent_at"] = now.isoformat()
        request.session["register_code_expires_at"] = expires.isoformat()
        request.session["register_code_attempts"] = 0
        request.session.modified = True
        return code, now, expires

    def send_new_account_code(username: str, email: str):
        """Send code for new registration (case 1.1)."""
        nonlocal sent, test_code, error

        is_temporary_reg = (reg_type == "temporary")

        # Rate limit: 1 code per 30 minutes (skip for temporary users)
        if not is_temporary_reg:
            last_sent_raw = request.session.get("register_code_sent_at")
            if last_sent_raw:
                try:
                    last_sent = timezone.datetime.fromisoformat(last_sent_raw)
                    elapsed = timezone.now() - last_sent
                    if elapsed < timedelta(minutes=30):
                        wait = 30 - int(elapsed.total_seconds() / 60)
                        error = f"Kod już wysłany. Poczekaj jeszcze około {wait} min przed kolejnym wysłaniem."
                        return
                except ValueError:
                    pass

        code, now, expires = _generate_code()

        if is_temporary_reg or getattr(settings, "TEST_MODE", False):
            sent = True
            test_code = code
            return

        sent_str = now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        valid_str = expires.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        from_addr = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum")
        send_mail(
            subject="[eudaHub] Kod rejestracyjny",
            message=(
                f"Twój kod rejestracyjny dla konta {username}: {code}\n\n"
                f"Wysłano: {sent_str}\n"
                f"Ważny do: {valid_str}\n\n"
                f"Jeśli to nie Ty — zignoruj tę wiadomość.\n\n"
                f"---\n"
                f"Jak rozpoznać nasze maile: temat zaczyna się od \"[eudaHub]\", "
                f"nadawca to {from_addr}.\n"
                f"Wiadomość mogła trafić do folderu Spam — sprawdź go jeśli nie widzisz maila."
            ),
            from_email=_from_email(),
            recipient_list=[email],
            fail_silently=False,
        )
        sent = True

    def send_recover_code(existing_user):
        """Send code + nick to email for account recovery (cases 1.3/1.5)."""
        nonlocal sent, test_code, test_nick

        code, now, expires = _generate_code()

        if getattr(settings, "TEST_MODE", False):
            sent = True
            test_code = code
            test_nick = existing_user.username
            return

        send_mail(
            subject="[eudaHub] Odzyskanie konta",
            message=(
                f"Ktoś próbował zarejestrować się na forum eudaHub z Twoim adresem email.\n\n"
                f"Twój nick: {existing_user.username}\n"
                f"Kod weryfikacyjny: {code}\n\n"
                f"Wpisz ten nick i kod na stronie rejestracji, aby odzyskać konto.\n\n"
                f"Jeśli to nie Ty — zignoruj tę wiadomość."
            ),
            from_email=_from_email(),
            recipient_list=[existing_user.email],
            fail_silently=False,
        )
        sent = True

    if request.method == "GET" and request.GET.get("reset") == "1":
        clear_pending_registration()
        pending = None

    # GeoIP country block check
    from .geoip import is_country_blocked, get_country_code
    client_ip = _get_client_ip(request)
    _reg_blocked_country = get_country_code(client_ip) if is_country_blocked(client_ip) else None

    if request.method == "POST":
        if _reg_blocked_country:
            error = f"Rejestracja niedostępna z Twojej lokalizacji ({_reg_blocked_country})."
            return render(request, "registration/register.html", {
                "start_form": start_form, "finish_form": finish_form,
                "sent": False, "error": error, "reg_type": None,
                "is_temp_mode": is_temp_mode, "test_code": None,
            })

        action = request.POST.get("action")

        if action == "start":
            chosen_type = request.POST.get("reg_type", "")
            if is_temp_mode and chosen_type not in ("real", "temporary"):
                error = "Wybierz typ konta: prawdziwe lub tymczasowe."
            else:
                # Validate format first
                start_form = RegisterStartForm(request.POST)
                if not start_form.is_valid():
                    pass  # form errors will be shown
                else:
                    username = start_form.cleaned_data["username"]
                    email = start_form.cleaned_data["email"]
                    is_temp_reg = (chosen_type == "temporary")

                    nick_owner = User.objects.filter(
                        username_normalized=normalize(username)
                    ).first()
                    email_owner = User.objects.filter(email=email).first() if not is_temp_reg else None

                    nick_taken = nick_owner is not None
                    email_taken = email_owner is not None

                    if is_temp_reg:
                        # Temporary accounts: only check nick
                        if nick_taken:
                            error = "Nick zajęty, wybierz inny nick."
                        else:
                            clear_pending_registration()
                            request.session["register_pending"] = {
                                "username": username,
                                "email": email,
                                "reg_type": "temporary",
                                "mode": "new",
                            }
                            request.session.modified = True
                            return redirect("register")

                    elif not nick_taken and not email_taken:
                        # 1.1: both free → new account
                        clear_pending_registration()
                        request.session["register_pending"] = {
                            "username": username,
                            "email": email,
                            "reg_type": chosen_type if is_temp_mode else "real",
                            "mode": "new",
                        }
                        request.session.modified = True
                        return redirect("register")

                    elif nick_taken and not email_taken:
                        # 1.2: nick taken, email free
                        error = "Nick zajęty, wybierz inny nick do rejestracji."

                    elif email_taken and not nick_taken:
                        # 1.3: email taken, nick free → recover
                        clear_pending_registration()
                        request.session["register_pending"] = {
                            "username": "",
                            "email": email,
                            "reg_type": "recover",
                            "mode": "recover",
                            "recover_username": email_owner.username,
                        }
                        request.session.modified = True
                        send_recover_code(email_owner)
                        return redirect("register")

                    else:
                        # Both taken
                        if nick_owner.pk == email_owner.pk:
                            # 1.4: same user → account exists
                            error = (
                                'Konto już istnieje; '
                                '<a href="/login/">zaloguj się</a> lub wybierz '
                                '<a href="/password-reset/">nie mam hasła</a>.'
                            )
                        else:
                            # 1.5: different users → recover by email
                            clear_pending_registration()
                            request.session["register_pending"] = {
                                "username": "",
                                "email": email,
                                "reg_type": "recover",
                                "mode": "recover",
                                "recover_username": email_owner.username,
                            }
                            request.session.modified = True
                            send_recover_code(email_owner)
                            return redirect("register")

        elif action == "send_code":
            if not pending:
                return redirect("register")
            mode = pending.get("mode", "new")
            if mode == "recover":
                # Resend recovery code
                recover_user = User.objects.filter(
                    username=pending.get("recover_username"),
                    email=pending["email"],
                ).first()
                if recover_user:
                    send_recover_code(recover_user)
            else:
                # New account: confirm email then send code
                email_confirm = request.POST.get("email_confirm", "").strip().lower()
                if email_confirm != pending["email"]:
                    error = "Podany adres email nie zgadza się. Wpisz pełny adres widoczny w masce."
                else:
                    send_new_account_code(pending["username"], pending["email"])

        elif action == "finish":
            if not pending:
                return redirect("register")
            finish_form = RegisterFinishForm(request.POST)
            code = request.session.get("register_code")
            expires_at_raw = request.session.get("register_code_expires_at")
            attempts = int(request.session.get("register_code_attempts", 0))
            expires_at = None
            if expires_at_raw:
                try:
                    expires_at = timezone.datetime.fromisoformat(expires_at_raw)
                except ValueError:
                    expires_at = None

            if finish_form.is_valid():
                mode = pending.get("mode", "new")
                is_recover = (mode == "recover")

                if is_recover:
                    # The nick entered must match the recover_username
                    entered_nick = request.POST.get("recover_nick", "").strip()
                    if entered_nick != pending.get("recover_username"):
                        error = "Wpisany nick nie zgadza się z kontem powiązanym z tym emailem."

                if not is_recover and not error:
                    # Re-check uniqueness at final submit for new accounts
                    username = pending["username"]
                    email = pending["email"]
                    conflict_name = User.objects.filter(
                        username_normalized=normalize(username)
                    ).exists()
                    conflict_email = User.objects.filter(email=email).exists()
                    if conflict_name:
                        clear_pending_registration()
                        error = "Taki nick został już zajęty w międzyczasie. Zacznij rejestrację od nowa."
                    elif conflict_email:
                        clear_pending_registration()
                        error = "Ten email został już użyty w międzyczasie. Zacznij rejestrację od nowa."

                if not error:
                    if not code or not expires_at or timezone.now() >= expires_at:
                        error = "Kod wygasł. Wyślij nowy kod."
                    elif attempts >= 10:
                        error = "Zbyt wiele błędnych prób kodu. Wyślij nowy kod."
                    elif finish_form.cleaned_data["code"] != code:
                        request.session["register_code_attempts"] = attempts + 1
                        request.session.modified = True
                        error = "Nieprawidłowy kod."
                    else:
                        password = finish_form.cleaned_data["password1"]
                        if is_recover:
                            # Claim existing account
                            user = User.objects.get(
                                username=pending["recover_username"],
                                email=pending["email"],
                            )
                            if finish_form.cleaned_data.get("password_is_prehashed") == "1":
                                user.set_password(password)
                            else:
                                user.set_password(prehash_password(password, user.username))
                            user.is_active = True
                            user.save()
                            clear_pending_registration()
                            login(request, user)
                            return redirect("/")
                        else:
                            is_temp_reg = (reg_type == "temporary")

                            # IP-based multi-account check
                            if cfg.reg_ip_limit:
                                window_hours = cfg.reg_ip_window_hours
                                max_accounts = cfg.reg_ip_max_temp if is_temp_reg else cfg.reg_ip_max_real
                                if max_accounts > 0:
                                    since = timezone.now() - timedelta(hours=window_hours)
                                    recent_count = User.objects.filter(
                                        registration_ip=client_ip,
                                        is_temporary=is_temp_reg,
                                        date_joined__gte=since,
                                    ).count()
                                if max_accounts > 0 and recent_count >= max_accounts:
                                    kind = "tymczasowych" if is_temp_reg else "realnych"
                                    error = (
                                        f"Z tego adresu IP zarejestrowano już {max_accounts} "
                                        f"kont{'' if max_accounts == 1 else 'a'} {kind} "
                                        f"w ciągu ostatnich {window_hours} godzin. "
                                        f"Spróbuj ponownie później."
                                    )

                            if not error:
                                user = User(
                                    username=pending["username"],
                                    email=pending["email"],
                                    is_active=True,
                                    is_temporary=is_temp_reg,
                                    registration_ip=client_ip,
                                )
                                if finish_form.cleaned_data.get("password_is_prehashed") == "1":
                                    user.set_password(password)
                                else:
                                    user.set_password(prehash_password(password, user.username))
                                user.save()
                                clear_pending_registration()
                                login(request, user)
                                return redirect("/")

    if pending:
        start_form = RegisterStartForm(initial=pending)

    email_mask = mask_email(pending["email"]) if pending else None

    code_valid_until = None
    if pending:
        raw = request.session.get("register_code_expires_at")
        if raw:
            try:
                code_valid_until = (
                    timezone.datetime.fromisoformat(raw)
                    .astimezone(_dt.timezone.utc)
                    .strftime("%H:%M UTC")
                )
            except ValueError:
                pass

    return render(request, "registration/register.html", {
        "start_form": start_form,
        "finish_form": finish_form,
        "pending": pending,
        "email_mask": email_mask,
        "sent": sent,
        "test_code": test_code,
        "test_nick": test_nick,
        "error": error,
        "code_valid_until": code_valid_until,
        "is_temp_mode": is_temp_mode,
        "reg_type": reg_type,
    })


def activate_ghost(request):
    """Step 2 of ghost activation: user proves email ownership."""
    from .models import User
    username = request.POST.get("username") or request.GET.get("username", "")
    try:
        user = User.objects.get(username=username, is_active=False)
    except User.DoesNotExist:
        return render(request, "registration/activate_ghost.html", {
            "username": username, "error": "Nie znaleziono konta oczekującego aktywacji.",
        })

    # Get or create token record (used for rate limiting)
    token_obj, _ = ActivationToken.objects.get_or_create(
        user=user,
        defaults={
            "token": secrets.token_urlsafe(48),
            "expires_at": timezone.now() + timedelta(hours=24),
        },
    )

    if request.method == "POST":
        if token_obj.is_rate_limited():
            remaining = ActivationToken.WINDOW_MINUTES
            return render(request, "registration/activate_ghost.html", {
                "username": username,
                "email_mask": mask_email(user.email) if user.email else None,
                "error": f"Zbyt wiele prób. Spróbuj ponownie za {remaining} minut.",
            })

        email_input = request.POST.get("email", "").strip().lower()
        if not user.email or email_input != user.email:
            token_obj.record_failed_attempt()
            remaining_attempts = ActivationToken.MAX_ATTEMPTS - token_obj.failed_attempts
            return render(request, "registration/activate_ghost.html", {
                "username": username,
                "email_mask": mask_email(user.email) if user.email else None,
                "error": f"Podany email nie pasuje do konta. Pozostało prób: {max(remaining_attempts, 0)}.",
            })

        if getattr(settings, "TEST_MODE", False):
            # TEST_MODE: activate immediately, no email link
            user.is_active = True
            user.save(update_fields=["is_active"])
            login(request, user)
            return render(request, "registration/activate_confirm.html", {
                "success": True, "username": user.username,
            })

        # Production: send activation link
        token_obj.token = secrets.token_urlsafe(48)
        token_obj.expires_at = timezone.now() + timedelta(hours=24)
        token_obj.failed_attempts = 0
        token_obj.window_start = None
        token_obj.save()

        activation_url = request.build_absolute_uri(f"/activate/{token_obj.token}/")
        send_mail(
            subject="Aktywacja konta",
            message=f"Kliknij link aby aktywować konto {username}:\n\n{activation_url}\n\nLink ważny 24 godziny.",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum"),
            recipient_list=[email_input],
            fail_silently=False,
        )
        return render(request, "registration/activate_ghost.html", {
            "username": username,
            "sent": True,
        })

    return render(request, "registration/activate_ghost.html", {
        "username": username,
        "email_mask": mask_email(user.email) if user.email else None,
    })


def find_account(request):
    """'Nie pamiętam nicka' — user podaje email, dostaje nick + link aktywacyjny na skrzynkę.

    Nigdy nie ujawniamy nicka na ekranie — tylko na email, żeby nikt nie szpiegował
    cudzych nicków przez wpisanie dowolnego maila.
    Odpowiedź jest zawsze taka sama (czy znaleziono konto czy nie) — nie zdradza
    czy dany email jest w bazie.
    """
    sent = False
    not_found = False
    if request.method == "POST":
        email_input = request.POST.get("email", "").strip().lower()
        if email_input:
            user = User.objects.filter(email=email_input).first()

            if user and user.is_ghost():
                # Ghost account — send activation link
                token_obj, _ = ActivationToken.objects.get_or_create(
                    user=user,
                    defaults={
                        "token": secrets.token_urlsafe(48),
                        "expires_at": timezone.now() + timedelta(hours=24),
                    },
                )
                token_obj.token = secrets.token_urlsafe(48)
                token_obj.expires_at = timezone.now() + timedelta(hours=24)
                token_obj.failed_attempts = 0
                token_obj.window_start = None
                token_obj.save()

                activation_url = request.build_absolute_uri(f"/activate/{token_obj.token}/")

                if getattr(settings, "TEST_MODE", False):
                    user.is_active = True
                    user.save(update_fields=["is_active"])
                    login(request, user)
                    return render(request, "registration/find_account.html", {
                        "test_mode_username": user.username,
                        "success": True,
                    })

                send_mail(
                    subject="[eudaHub] Twoje konto na forum",
                    message=(
                        f"Znaleźliśmy Twoje konto na forum.\n\n"
                        f"Twój nick: {user.username}\n\n"
                        f"Kliknij link poniżej aby aktywować konto:\n{activation_url}\n\n"
                        f"Link ważny 24 godziny.\n"
                        f"Jeśli to nie Ty — zignoruj tę wiadomość."
                    ),
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum"),
                    recipient_list=[email_input],
                    fail_silently=True,
                )
                sent = True
            elif user:
                # Active account — send nick reminder + password reset hint
                reset_url = request.build_absolute_uri("/password-reset/")
                if getattr(settings, "TEST_MODE", False):
                    return render(request, "registration/find_account.html", {
                        "test_mode_username": user.username,
                        "test_mode_active": True,
                        "success": True,
                    })

                send_mail(
                    subject="[eudaHub] Twoje konto na forum",
                    message=(
                        f"Twój nick na forum: {user.username}\n\n"
                        f"Konto jest aktywne. Aby się zalogować, użyj tego nicka.\n"
                        f"Jeśli nie pamiętasz hasła, zresetuj je tutaj:\n{reset_url}\n\n"
                        f"Jeśli to nie Ty — zignoruj tę wiadomość."
                    ),
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum"),
                    recipient_list=[email_input],
                    fail_silently=True,
                )
                sent = True
            else:
                not_found = True

    return render(request, "registration/find_account.html", {"sent": sent, "not_found": not_found})


def activate_confirm(request, token):
    """Final step: user clicks email link, account activated."""
    from .models import User
    try:
        token_obj = ActivationToken.objects.select_related("user").get(token=token)
    except ActivationToken.DoesNotExist:
        return render(request, "registration/activate_confirm.html", {"invalid": True})

    if not token_obj.is_valid():
        token_obj.delete()
        return render(request, "registration/activate_confirm.html", {"expired": True})

    user = token_obj.user
    user.is_active = True
    user.save(update_fields=["is_active"])
    token_obj.delete()
    login(request, user)
    return render(request, "registration/activate_confirm.html", {"success": True, "username": user.username})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _retain_until(flagged: bool) -> "datetime":
    """Calculate IP retention deadline from current time."""
    days = (
        getattr(settings, "IP_RETAIN_DANGEROUS_DAYS", 90)
        if flagged
        else getattr(settings, "IP_RETAIN_NORMAL_DAYS", 30)
    )
    return timezone.now() + timedelta(days=days)


def _is_moderator(user, forum) -> bool:
    """True for root, global admins/moderators (role≥1), and forum-specific moderators."""
    if not user.is_authenticated:
        return False
    return (
        user.is_root
        or user.role >= User.ROLE_MODERATOR
        or forum.moderators.filter(pk=user.pk).exists()
    )


def _post_page(post: Post) -> int:
    per_page = getattr(settings, "POSTS_PER_PAGE", 20)
    return (post.post_order - 1) // per_page + 1


# ---------------------------------------------------------------------------
# Admin: blocked IP management (root only)
# ---------------------------------------------------------------------------

@login_required
def admin_blocked_ips(request):
    if not request.user.is_root:
        return HttpResponseForbidden()

    error = None

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            ip = request.POST.get("ip", "").strip()
            reason = request.POST.get("reason", "").strip()
            if not ip:
                error = "Podaj adres IP."
            else:
                try:
                    _, created = BlockedIP.objects.get_or_create(
                        ip_address=ip,
                        defaults={"reason": reason, "added_by": request.user},
                    )
                    if not created:
                        error = f"{ip} już jest na liście."
                    else:
                        invalidate_blocked_ips_cache()
                except Exception:
                    error = "Nieprawidłowy adres IP."

        elif action == "delete":
            BlockedIP.objects.filter(pk=request.POST.get("ip_id")).delete()
            invalidate_blocked_ips_cache()
            return redirect("admin_blocked_ips")

    blocked = BlockedIP.objects.select_related("added_by").order_by("-added_at")
    return render(request, "board/admin_blocked_ips.html", {
        "blocked": blocked,
        "error": error,
    })


# ---------------------------------------------------------------------------
# Moderator: flag post as dangerous — extends IP retention period (!)
# ---------------------------------------------------------------------------

@login_required
def flag_post_ip(request, post_id):
    """Moderator clicks ! — marks post as dangerous, extends IP retention to 90d."""
    if request.method != "POST":
        return HttpResponseForbidden()

    post = get_object_or_404(Post.objects.select_related("topic__forum"), pk=post_id)

    if not _is_moderator(request.user, post.topic.forum):
        return HttpResponseForbidden()

    if not post.ip_flagged:
        post.ip_flagged = True
        post.ip_retain_until = _retain_until(flagged=True)
        post.save(update_fields=["ip_flagged", "ip_retain_until"])

    return redirect(f"/topic/{post.topic.pk}/?page={_post_page(post)}#post-{post.pk}")


# ---------------------------------------------------------------------------
# Moderator: spam action panel
# ---------------------------------------------------------------------------

@login_required
def spam_action(request, post_id):
    """Moderator spam panel: delete post, ban user, release nick, flag IP, block domain."""
    post = get_object_or_404(Post.objects.select_related("author", "topic__forum"), pk=post_id)
    topic = post.topic
    forum = topic.forum
    user = request.user

    if not _is_moderator(user, forum):
        return HttpResponseForbidden()

    author = post.author

    # Email domain info
    email_domain = None
    domain_users = []
    domain_user_count = 0
    domain_already_blocked = False
    if author and author.email:
        try:
            import tldextract
            from .models import SpamDomain
            ext = tldextract.extract(author.email.split("@")[-1])
            if ext.domain and ext.suffix:
                email_domain = f"{ext.domain}.{ext.suffix}"
                domain_users = list(
                    User.objects.filter(email__iendswith=f"@{email_domain}")
                    .order_by("-post_count")
                    .values("pk", "username", "post_count", "date_joined")
                )
                domain_user_count = len(domain_users)
                domain_already_blocked = SpamDomain.objects.filter(
                    domain=email_domain, spam=1
                ).exists()
        except Exception:
            pass

    if request.method == "POST":
        do_delete = request.POST.get("do_delete") == "1"
        ban_duration = request.POST.get("ban_duration", "0")
        do_release_nick = request.POST.get("do_release_nick") == "1"
        do_flag_ip = request.POST.get("do_flag_ip") == "1"
        do_block_domain = request.POST.get("do_block_domain") == "1"

        topic_deleted = False
        with transaction.atomic():
            # 1. Delete post (skip if releasing nick — that deletes all posts anyway)
            if do_delete and not do_release_nick:
                if topic.posts.count() == 1:
                    # Last post — delete entire topic
                    if author:
                        User.objects.filter(pk=author.pk, post_count__gt=0).update(
                            post_count=django_models.F("post_count") - 1
                        )
                    topic.delete()
                    topic_deleted = True
                else:
                    deleted_order = post.post_order
                    if author:
                        User.objects.filter(pk=author.pk, post_count__gt=0).update(
                            post_count=django_models.F("post_count") - 1
                        )
                    post.delete()
                    topic.posts.filter(post_order__gt=deleted_order).update(
                        post_order=django_models.F("post_order") - 1
                    )
                    new_last = topic.posts.order_by("-created_at").first()
                    if new_last:
                        topic.reply_count = topic.posts.count() - 1
                        topic.last_post = new_last
                        topic.last_post_at = new_last.created_at
                        topic.save(update_fields=["reply_count", "last_post", "last_post_at"])
                forum.post_count = Post.objects.filter(topic__forum=forum).count()
                forum.topic_count = forum.topics.count()
                new_forum_last = Post.objects.filter(topic__forum=forum).order_by("-created_at").first()
                forum.last_post = new_forum_last
                forum.last_post_at = new_forum_last.created_at if new_forum_last else None
                forum.save(update_fields=["post_count", "topic_count", "last_post", "last_post_at"])

            # 2. Flag IP as dangerous
            if do_flag_ip and not do_delete:
                # post was not deleted — update ip fields on it
                post.ip_flagged = True
                post.ip_retain_until = _retain_until(flagged=True)
                post.save(update_fields=["ip_flagged", "ip_retain_until"])
            elif do_flag_ip and do_delete:
                # post deleted — flag all remaining posts by this IP
                if post.author_ip:
                    Post.objects.filter(author_ip=post.author_ip).update(
                        ip_flagged=True,
                        ip_retain_until=_retain_until(flagged=True),
                    )

            # 3. Ban user
            if author and ban_duration != "0":
                author.is_active = False
                if ban_duration == "forever":
                    author.banned_until = None
                else:
                    author.banned_until = timezone.now() + timedelta(days=int(ban_duration))
                author.save(update_fields=["is_active", "banned_until"])

            # 4. Release nick (delete user account + all posts + quote cleanup)
            is_admin_or_root = user.is_root or user.role >= User.ROLE_ADMIN
            if author and do_release_nick:
                if not is_admin_or_root and author.active_days > 5:
                    # Moderators may only release new/spam users (active_days <= 5)
                    pass
                else:
                    if author.email:
                        from .models import SpamEmail
                        SpamEmail.objects.get_or_create(email=author.email.strip().lower())
                    from .user_delete import delete_user_and_cleanup
                    delete_user_and_cleanup(author)

            # 5. Block email domain
            if do_block_domain and email_domain:
                from .models import SpamDomain
                SpamDomain.objects.update_or_create(
                    domain=email_domain,
                    defaults={"spam": 1, "added_at": timezone.now()},
                )

        messages.success(request, "Akcja antyspamowa wykonana.")
        if topic_deleted or do_release_nick:
            return redirect("forum_detail", forum_id=forum.pk)
        return redirect("topic_detail", topic_id=topic.pk)

    ban_choices = [
        ("0",       "Nie blokuj"),
        ("1",       "1 dzień"),
        ("7",       "7 dni"),
        ("30",      "30 dni"),
        ("90",      "90 dni"),
        ("forever", "Na zawsze"),
    ]

    is_admin_or_root = user.is_root or user.role >= User.ROLE_ADMIN
    can_release_nick = is_admin_or_root or (author is None) or (author.active_days <= 5)

    # GeoIP
    from .geoip import get_country_info, is_country_blocked
    from .models import BlockedCountry
    ip_country_code, ip_country_name = get_country_info(post.author_ip)
    ip_country_blocked = is_country_blocked(post.author_ip) if post.author_ip else False

    return render(request, "board/spam_action.html", {
        "post": post,
        "topic": topic,
        "author": author,
        "email_domain": email_domain,
        "domain_users": domain_users,
        "domain_user_count": domain_user_count,
        "domain_already_blocked": domain_already_blocked,
        "dangerous_days": getattr(settings, "IP_RETAIN_DANGEROUS_DAYS", 90),
        "ban_choices": ban_choices,
        "can_release_nick": can_release_nick,
        "ip_country_code": ip_country_code,
        "ip_country_name": ip_country_name,
        "ip_country_blocked": ip_country_blocked,
        "is_admin_or_root": is_admin_or_root,
    })


# ---------------------------------------------------------------------------
# Moderation queue — pending posts from new users during moderation windows
# ---------------------------------------------------------------------------

@login_required
def moderation_queue(request):
    """Flat list of pending posts awaiting moderation. Actions: approve / delete / release user."""
    user = request.user
    if not (user.is_root or user.role >= User.ROLE_MODERATOR):
        return HttpResponseForbidden()

    if request.method == "POST":
        action = request.POST.get("action")
        post_ids = [int(x) for x in request.POST.getlist("post_ids") if x.isdigit()]
        is_admin_or_root = user.is_root or user.role >= User.ROLE_ADMIN

        if action == "approve" and post_ids:
            posts = list(
                Post.objects
                .filter(pk__in=post_ids, is_pending=True)
                .select_related("author", "topic__forum")
            )
            with transaction.atomic():
                for post in posts:
                    post.is_pending = False
                    post.save(update_fields=["is_pending"])
                    topic = post.topic
                    forum = topic.forum
                    if topic.is_pending:
                        topic.is_pending = False
                        topic.save(update_fields=["is_pending"])
                    _update_topic_stats(topic, post)
                    _update_forum_stats(forum, post)
                    if post.author:
                        _increment_user_post_count(post.author)
                        from .active_days import increment_if_new_day
                        increment_if_new_day(post.author, post)
            messages.success(request, f"Zatwierdzono {len(posts)} post(ów).")

        elif action == "delete" and post_ids:
            posts = list(
                Post.objects
                .filter(pk__in=post_ids, is_pending=True)
                .select_related("topic")
            )
            with transaction.atomic():
                topic_ids = set(p.topic_id for p in posts)
                Post.objects.filter(pk__in=[p.pk for p in posts]).delete()
                # Remove topics that have no posts left
                for tid in topic_ids:
                    try:
                        t = Topic.objects.get(pk=tid)
                        if not t.posts.exists():
                            t.delete()
                    except Topic.DoesNotExist:
                        pass
            messages.success(request, f"Usunięto {len(posts)} post(ów).")

        elif action == "release_user":
            user_id = request.POST.get("user_id", "").strip()
            if user_id.isdigit():
                target = get_object_or_404(User, pk=int(user_id))
                if not is_admin_or_root and target.active_days > 5:
                    messages.error(
                        request,
                        f"Brak uprawnień — moderator nie może usuwać użytkowników z active_days > 5 "
                        f"({target.username} ma {target.active_days} dni aktywności).",
                    )
                else:
                    if target.email:
                        from .models import SpamEmail
                        SpamEmail.objects.get_or_create(email=target.email.strip().lower())
                    from .user_delete import delete_user_and_cleanup
                    delete_user_and_cleanup(target)
                    messages.success(request, f'Konto \u201e{target.username}\u201c zostało usunięte.')

        return redirect("moderation_queue")

    pending_posts = (
        Post.objects
        .filter(is_pending=True)
        .select_related("author", "topic", "topic__forum")
        .order_by("created_at")
    )
    # Group authors for the "release user" action
    author_ids = pending_posts.values_list("author_id", flat=True).distinct()
    authors_in_queue = (
        User.objects
        .filter(pk__in=author_ids)
        .order_by("username")
        .values("pk", "username", "active_days", "post_count")
    )
    is_admin_or_root = user.is_root or user.role >= User.ROLE_ADMIN
    return render(request, "board/moderation_queue.html", {
        "pending_posts": pending_posts,
        "authors_in_queue": list(authors_in_queue),
        "is_admin_or_root": is_admin_or_root,
    })


# ---------------------------------------------------------------------------
# Moderation windows config — admin-only CRUD
# ---------------------------------------------------------------------------

@login_required
def moderation_windows_config(request):
    """Admin-only view to add/delete time windows for the moderation queue."""
    user = request.user
    if not (user.is_root or user.role >= User.ROLE_ADMIN):
        return HttpResponseForbidden()

    from .models import ModerationWindow
    DAYS = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            try:
                start_hour = int(request.POST["start_hour"])
                start_minute = int(request.POST.get("start_minute", 0))
                end_hour = int(request.POST["end_hour"])
                end_minute = int(request.POST.get("end_minute", 0))
                tz_name = request.POST.get("timezone", "Europe/Warsaw").strip() or "Europe/Warsaw"
                day_from_raw = request.POST.get("day_from", "").strip()
                day_to_raw = request.POST.get("day_to", "").strip()
                day_from = int(day_from_raw) if day_from_raw.isdigit() else None
                day_to = int(day_to_raw) if day_to_raw.isdigit() else None
                if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59
                        and 0 <= end_hour <= 23 and 0 <= end_minute <= 59):
                    raise ValueError("Invalid time")
                ModerationWindow.objects.create(
                    start_hour=start_hour,
                    start_minute=start_minute,
                    end_hour=end_hour,
                    end_minute=end_minute,
                    day_from=day_from,
                    day_to=day_to,
                    timezone=tz_name,
                    is_active=True,
                    created_by=user,
                )
                messages.success(request, "Okno moderacji zostało dodane.")
            except (KeyError, ValueError) as exc:
                messages.error(request, f"Błąd danych: {exc}")

        elif action == "toggle":
            wid = request.POST.get("window_id", "")
            if wid.isdigit():
                try:
                    w = ModerationWindow.objects.get(pk=int(wid))
                    w.is_active = not w.is_active
                    w.save(update_fields=["is_active"])
                except ModerationWindow.DoesNotExist:
                    pass

        elif action == "delete":
            wid = request.POST.get("window_id", "")
            if wid.isdigit():
                ModerationWindow.objects.filter(pk=int(wid)).delete()
                messages.success(request, "Okno moderacji zostało usunięte.")

        return redirect("moderation_windows_config")

    windows = ModerationWindow.objects.all()
    return render(request, "board/moderation_windows.html", {
        "windows": windows,
        "days": list(enumerate(DAYS)),
    })


# ---------------------------------------------------------------------------
# Country blocks config — admin-only
# ---------------------------------------------------------------------------

@login_required
def country_blocks_config(request):
    """Admin view to block/unblock countries by ISO 3166-1 alpha-2 code."""
    user = request.user
    if not (user.is_root or user.role >= User.ROLE_ADMIN):
        return HttpResponseForbidden()

    from .models import BlockedCountry
    from .geoip import get_country_info

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            code = request.POST.get("country_code", "").strip().upper()[:2]
            if len(code) == 2 and code.isalpha():
                # Try to get name from GeoIP or from form
                name = request.POST.get("country_name", "").strip()
                BlockedCountry.objects.get_or_create(
                    country_code=code,
                    defaults={"country_name": name, "blocked_by": user},
                )
                messages.success(request, f"Kraj {code} został zablokowany.")
            else:
                messages.error(request, "Nieprawidłowy kod kraju (wymagane 2 litery, np. PL).")

        elif action == "add_by_ip":
            ip = request.POST.get("ip", "").strip()
            code, name = get_country_info(ip)
            if code:
                BlockedCountry.objects.get_or_create(
                    country_code=code,
                    defaults={"country_name": name or "", "blocked_by": user},
                )
                messages.success(request, f"Zablokowano kraj {code} ({name or '?'}) na podstawie IP {ip}.")
            else:
                messages.error(request, f"Nie udało się ustalić kraju dla IP {ip} (brak bazy GeoIP lub nieznane IP).")

        elif action == "delete":
            code = request.POST.get("country_code", "").strip().upper()
            BlockedCountry.objects.filter(country_code=code).delete()
            messages.success(request, f"Kraj {code} odblokowany.")

        return redirect("country_blocks_config")

    blocked = list(BlockedCountry.objects.all())
    return render(request, "board/country_blocks.html", {
        "blocked": blocked,
    })


# ---------------------------------------------------------------------------
# Auth: custom login (detects invalidated password) + password reset via code
# ---------------------------------------------------------------------------

def _generate_reset_code() -> str:
    """Return a cryptographically random 6-digit string."""
    return f"{secrets.randbelow(900000) + 100000}"


def _can_send_reset_code(user) -> tuple[bool, int]:
    """Check rate limit. Returns (allowed, codes_sent_this_hour)."""
    since = timezone.now() - timedelta(hours=1)
    sent = PasswordResetCode.objects.filter(user=user, created_at__gte=since).count()
    return sent < PasswordResetCode.MAX_PER_HOUR, sent


def _find_valid_code(user, code_input: str):
    """Return the matching PasswordResetCode if valid, else None.

    Accepts:
    - The most recent unused+unexpired code — always.
    - The second most recent — only if created within GRACE_MINUTES ago
      (handles impatient 'resend' when the first email is just delayed).
    """
    now = timezone.now()
    candidates = list(
        PasswordResetCode.objects.filter(
            user=user, is_used=False, expires_at__gt=now
        ).order_by("-created_at")[:2]
    )
    if not candidates:
        return None

    # Latest code
    if candidates[0].code == code_input:
        return candidates[0]

    # Previous code within grace window
    if len(candidates) == 2:
        prev = candidates[1]
        if prev.code == code_input:
            grace_cutoff = now - timedelta(minutes=PasswordResetCode.GRACE_MINUTES)
            if prev.created_at >= grace_cutoff:
                return prev

    return None


def _from_email() -> str:
    name = getattr(settings, "EMAIL_FROM_NAME", "Forum")
    addr = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum")
    return f"{name} <{addr}>"


def _send_reset_code_email(user, code: str, recipient_email: str) -> None:
    now     = timezone.now()
    expires = now + timedelta(hours=PasswordResetCode.CODE_EXPIRY_HOURS)
    sent_str  = now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    valid_str = expires.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    from_addr = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum")
    send_mail(
        subject="[eudaHub] Kod do resetowania hasła",
        message=(
            f"Nick: {user.username}\n"
            f"Kod: {code}\n"
            f"Wysłano: {sent_str}\n"
            f"Ważny do: {valid_str}\n\n"
            f"Wejdź na forum → Zresetuj hasło i wpisz ten kod razem z nowym hasłem.\n\n"
            f"Jeśli to nie Ty prosiłeś — zignoruj tę wiadomość.\n\n"
            f"---\n"
            f"Jak rozpoznać nasze maile: temat zaczyna się od \"[eudaHub]\", "
            f"nadawca to {from_addr}.\n"
            f"Wiadomość mogła trafić do folderu Spam — sprawdź go jeśli nie widzisz maila."
        ),
        from_email=_from_email(),
        recipient_list=[recipient_email],
        fail_silently=False,
    )


_LOGIN_MAX_FAILS = 20   # per username per hour


def _login_fail_key(username: str) -> str:
    return f"login_fails:{username.lower()}"


def _check_login_rate(username: str) -> int:
    """Return number of failed attempts in last hour for this username."""
    from django.core.cache import cache
    return cache.get(_login_fail_key(username), 0)


def _record_login_fail(username: str) -> int:
    """Increment failed-login counter (1h window). Returns new count."""
    from django.core.cache import cache
    key = _login_fail_key(username)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=3600)
        return 1


def login_view(request):
    """Custom login.

    Three distinct error messages depending on the situation:
    - Nick not found → "Użytkownik nie istnieje"
    - Unusable password (ghost, reset) → "Hasło zostało zresetowane"
    - Wrong password → "Hasło się nie zgadza"
    """
    from django.contrib.auth import authenticate
    from .models import User as ForumUser

    if request.user.is_authenticated:
        return redirect("/")

    error = None

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        # Rate limit check before doing any DB work
        fails = _check_login_rate(username)
        if fails >= _LOGIN_MAX_FAILS:
            error = "Zbyt wiele nieudanych prób logowania. Spróbuj ponownie za godzinę lub zresetuj hasło."
        else:
            user = authenticate(request, username=username, password=password)
            if user is not None:
                if request.session.pop("ban_lifted", False):
                    from django.contrib import messages as django_messages
                    django_messages.success(request, "Twoje konto jest znów aktywne.")
                login(request, user)
                return redirect(request.POST.get("next") or request.GET.get("next") or "/")

            # Auth failed — distinguish cases
            try:
                candidate = ForumUser.objects.get(username=username)
                if candidate.is_root:
                    error = 'Nieprawidłowe hasło.'
                elif not candidate.is_active:
                    if candidate.banned_until is not None:
                        until = candidate.banned_until.strftime("%Y-%m-%d %H:%M")
                        error = f'Konto zablokowane do {until}.'
                    else:
                        error = 'Konto zablokowane bezterminowo.'
                elif not candidate.has_usable_password():
                    error = 'Hasło zostało zresetowane; wybierz <a href="/password-reset/">nie mam hasła</a>.'
                else:
                    error = 'Hasło się nie zgadza; podaj hasło albo wybierz <a href="/password-reset/">nie mam hasła</a>.'
            except ForumUser.DoesNotExist:
                error = 'Użytkownik nie istnieje; <a href="/register/">zarejestruj się</a>.'

            _record_login_fail(username)

    return render(request, "registration/login.html", {
        "error": error,
        "next": request.GET.get("next", ""),
    })


def request_reset(request):
    """'Nie mam hasła' — unified flow for ghost and regular accounts.

    Step 1 (check): nick → return email mask (AJAX)
    Step 2 (send):  verify email matches → send 6-digit code
    The do_reset view handles step 3 (code + new password).
    Ghost and non-ghost accounts are treated identically — no activation links.
    """
    from django.http import JsonResponse
    from .models import User as ForumUser

    prefill_username = request.GET.get("username", "")

    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        action = request.POST.get("action", "send")  # "check" | "send"
        username = request.POST.get("username", "").strip()

        def ajax_err(msg):
            return JsonResponse({"ok": False, "error": msg})

        try:
            user = ForumUser.objects.get(username=username)
        except ForumUser.DoesNotExist:
            msg = 'Nie znaleziono konta o tym nicku. <a href="/register/">Zarejestruj się.</a>'
            if is_ajax:
                return ajax_err(msg)
            return render(request, "registration/request_reset.html", {
                "error": msg, "prefill_username": username,
            })

        if user.is_root:
            msg = "Konto root nie korzysta z odzyskiwania hasła."
            if is_ajax:
                return ajax_err(msg)
            return render(request, "registration/request_reset.html", {
                "error": msg, "prefill_username": username,
            })

        if user.is_temporary:
            # Temporary accounts: skip email entirely — generate code and show on screen.
            allowed, _ = _can_send_reset_code(user)
            if not allowed:
                msg = (f"Wysłano już {PasswordResetCode.MAX_PER_HOUR} kody w ciągu ostatniej godziny. "
                       "Spróbuj ponownie za chwilę.")
                if is_ajax:
                    return ajax_err(msg)
                return render(request, "registration/request_reset.html", {
                    "error": msg, "prefill_username": username,
                })
            code = _generate_reset_code()
            expires = timezone.now() + timedelta(hours=PasswordResetCode.CODE_EXPIRY_HOURS)
            PasswordResetCode.objects.create(user=user, code=code, expires_at=expires)
            sent_at = timezone.now().strftime("%Y-%m-%d %H:%M")
            if is_ajax:
                return JsonResponse({
                    "ok": True, "greencode": True,
                    "code": code, "username": username, "sent_at": sent_at,
                })
            return render(request, "registration/request_reset.html", {
                "greencode_code": code, "greencode_username": username, "greencode_sent_at": sent_at,
            })

        if not user.email:
            msg = "To konto nie ma adresu email. Skontaktuj się z administratorem."
            if is_ajax:
                return ajax_err(msg)
            return render(request, "registration/request_reset.html", {
                "error": msg, "prefill_username": username,
            })

        # Step 1: just return email mask (no code sent)
        if action == "check":
            return JsonResponse({"ok": True, "email_mask": mask_email(user.email)})

        # Step 2: validate email confirmation before sending code
        email_confirm = request.POST.get("email_confirm", "").strip().lower()
        if email_confirm != user.email.lower():
            if is_ajax:
                return ajax_err("Podany adres email nie zgadza się.")
            return render(request, "registration/request_reset.html", {
                "error": "Podany adres email nie zgadza się.",
                "prefill_username": username,
            })

        allowed, _ = _can_send_reset_code(user)
        if not allowed:
            msg = (f"Wysłano już {PasswordResetCode.MAX_PER_HOUR} kody w ciągu ostatniej godziny. "
                   "Sprawdź skrzynkę lub spróbuj ponownie za chwilę.")
            if is_ajax:
                return ajax_err(msg)
            return render(request, "registration/request_reset.html", {
                "error": msg, "prefill_username": username,
            })

        code = _generate_reset_code()
        expires = timezone.now() + timedelta(hours=PasswordResetCode.CODE_EXPIRY_HOURS)
        PasswordResetCode.objects.create(user=user, code=code, expires_at=expires)

        use_greencode = getattr(settings, "TEST_MODE", False)

        if use_greencode:
            sent_at = timezone.now().strftime("%Y-%m-%d %H:%M")
            if is_ajax:
                return JsonResponse({
                    "ok": True,
                    "greencode": True,
                    "code": code,
                    "username": username,
                    "sent_at": sent_at,
                    "email_mask": mask_email(user.email),
                })
            return render(request, "registration/request_reset.html", {
                "greencode_code": code,
                "greencode_username": username,
                "greencode_sent_at": sent_at,
                "email_mask": mask_email(user.email),
            })

        _send_reset_code_email(user, code, user.email)
        if is_ajax:
            return JsonResponse({"ok": True, "greencode": False,
                                 "email_mask": mask_email(user.email)})
        return render(request, "registration/request_reset.html", {
            "sent": True, "email_mask": mask_email(user.email),
        })

    return render(request, "registration/request_reset.html", {
        "prefill_username": prefill_username,
    })


def do_reset(request):
    """Step 2: user enters username + new password × 2 + the code."""
    from .models import User as ForumUser

    error = None
    success = False
    prefill_username = request.GET.get("username", "")
    if request.method == "POST":
        username  = request.POST.get("username", "").strip()
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")
        code_input = request.POST.get("code", "").strip()

        if not username or not password1 or not password2 or not code_input:
            error = "Wypełnij wszystkie pola."
        elif password1 != password2:
            error = "Hasła nie są zgodne."
        else:
            try:
                user = ForumUser.objects.get(username=username)
            except ForumUser.DoesNotExist:
                error = "Nieprawidłowy nick lub kod."
            else:
                if user.is_root:
                    error = "Konto root nie korzysta z odzyskiwania hasła."
                elif (code_obj := _find_valid_code(user, code_input)) is None:
                    error = "Nieprawidłowy lub wygasły kod."
                else:
                    from .auth_utils import prehash_password
                    is_prehashed = request.POST.get("password_is_prehashed") == "1"
                    if not is_prehashed:
                        password1 = prehash_password(password1, username)
                    user.set_password(password1)
                    # Ghost accounts get activated on password reset (user proved email access)
                    update_fields = ["password", "is_active"]
                    user.is_active = True
                    user.save(update_fields=update_fields)
                    # Mark this and all older codes as used
                    PasswordResetCode.objects.filter(user=user, is_used=False).update(is_used=True)
                    login(request, user)
                    success = True

    return render(request, "registration/do_reset.html", {
        "error": error,
        "success": success,
        "prefill_username": prefill_username,
    })


# ---------------------------------------------------------------------------
# Private Messages
# ---------------------------------------------------------------------------

def _pm_counts(user):
    """Return (inbox_count, outbox_count, sent_count) for user."""
    boxes = (
        PrivateMessageBox.objects.filter(owner=user)
        .values("box_type")
        .annotate(n=django_models.Count("id"))
    )
    counts = {row["box_type"]: row["n"] for row in boxes}
    return (
        counts.get("INBOX",  0),
        counts.get("OUTBOX", 0),
        counts.get("SENT",   0),
    )


def _deliver_pending(user):
    """Deliver all OUTBOX messages addressed to user that fit in their inbox."""
    inbox_limit = getattr(settings, "PM_INBOX_LIMIT", 300)
    inbox_count = PrivateMessageBox.objects.filter(
        owner=user, box_type=PrivateMessageBox.BoxType.INBOX
    ).count()
    free = inbox_limit - inbox_count
    if free <= 0:
        return 0

    pending = PrivateMessage.objects.filter(
        recipient=user, delivered_at=None
    ).select_related("sender").order_by("created_at")[:free]

    delivered = 0
    now = timezone.now()
    for pm in pending:
        # Move sender's box: OUTBOX → SENT
        PrivateMessageBox.objects.filter(
            message=pm, box_type=PrivateMessageBox.BoxType.OUTBOX
        ).update(box_type=PrivateMessageBox.BoxType.SENT)
        # Create recipient's INBOX entry
        PrivateMessageBox.objects.create(
            message=pm,
            owner=user,
            box_type=PrivateMessageBox.BoxType.INBOX,
            is_read=False,
        )
        pm.delivered_at = now
        pm.save(update_fields=["delivered_at"])
        delivered += 1
    return delivered


@login_required
def pm_inbox(request):
    _deliver_pending(request.user)
    inbox_count, outbox_count, sent_count = _pm_counts(request.user)
    boxes = (
        PrivateMessageBox.objects.filter(
            owner=request.user, box_type=PrivateMessageBox.BoxType.INBOX
        )
        .select_related("message__sender")
        .order_by("-message__delivered_at")
    )
    return render(request, "board/pm_inbox.html", {
        "boxes": boxes,
        "inbox_count": inbox_count,
        "outbox_count": outbox_count,
        "sent_count": sent_count,
        "inbox_limit": getattr(settings, "PM_INBOX_LIMIT", 300),
    })


@login_required
def pm_outbox(request):
    _, outbox_count, sent_count = _pm_counts(request.user)
    inbox_count = PrivateMessageBox.objects.filter(
        owner=request.user, box_type=PrivateMessageBox.BoxType.INBOX
    ).count()
    boxes = (
        PrivateMessageBox.objects.filter(
            owner=request.user, box_type=PrivateMessageBox.BoxType.OUTBOX
        )
        .select_related("message__recipient")
        .order_by("-message__created_at")
    )
    return render(request, "board/pm_outbox.html", {
        "boxes": boxes,
        "inbox_count": inbox_count,
        "outbox_count": outbox_count,
        "sent_count": sent_count,
        "outbox_limit": getattr(settings, "PM_OUTBOX_LIMIT", 50),
    })


@login_required
def pm_sent(request):
    inbox_count, outbox_count, sent_count = _pm_counts(request.user)
    boxes = (
        PrivateMessageBox.objects.filter(
            owner=request.user, box_type=PrivateMessageBox.BoxType.SENT
        )
        .select_related("message__recipient")
        .order_by("-message__delivered_at")
    )
    return render(request, "board/pm_sent.html", {
        "boxes": boxes,
        "inbox_count": inbox_count,
        "outbox_count": outbox_count,
        "sent_count": sent_count,
        "sent_limit": getattr(settings, "PM_SENT_LIMIT", 300),
    })


@login_required
def pm_view(request, box_id):
    box = get_object_or_404(
        PrivateMessageBox.objects.select_related(
            "message__sender", "message__recipient"
        ),
        pk=box_id, owner=request.user,
    )
    if box.box_type == PrivateMessageBox.BoxType.INBOX and not box.is_read:
        box.is_read = True
        box.save(update_fields=["is_read"])
    return render(request, "board/pm_view.html", {"box": box})


@login_required
def pm_compose(request):
    from .pm_utils import compress, compress_from_b64
    from .models import User as ForumUser

    error = None
    prefill_recipient = request.GET.get("to", "")
    prefill_subject   = request.GET.get("subject", "")
    prefill_content   = request.GET.get("content", "")  # pre-filled quote for reply

    if request.method == "POST":
        recipient_name = request.POST.get("recipient", "").strip()
        subject        = request.POST.get("subject",   "").strip()
        raw_content    = request.POST.get("content",   "")
        b64_compressed = request.POST.get("content_compressed", "").strip()

        if not recipient_name or not subject or not raw_content:
            error = "Wypełnij wszystkie pola."
        else:
            try:
                recipient = ForumUser.objects.get(username=recipient_name, is_active=True)
            except ForumUser.DoesNotExist:
                error = f'Użytkownik "{recipient_name}" nie istnieje.'
            else:
                # Anti-spam: PM antiflood check
                from .antiflood import check_can_send_pm
                cfg_pm = SiteConfig.get()
                flood = check_can_send_pm(request.user, recipient, cfg_pm)
                if not flood["allowed"]:
                    error = flood["message"]
                # Anti-spam: check sender's outbox limit
                if not error:
                    outbox_limit = getattr(settings, "PM_OUTBOX_LIMIT", 50)
                    in_flight = PrivateMessage.objects.filter(
                        sender=request.user, delivered_at=None
                    ).count()
                    if in_flight >= outbox_limit:
                        error = (
                            f"Masz już {in_flight} wiadomości oczekujących na dostarczenie "
                            f"(limit: {outbox_limit}). Poczekaj aż odbiorcy je odbiorą."
                        )
                if not error:
                    repaired_content, changes, lint_errors = validate_pm_content(raw_content)
                    if lint_errors:
                        error_lines = "\n".join(f"• {e}" for e in lint_errors)
                        error = f"Błędy w kodzie BBCode:\n{error_lines}"
                    else:
                        raw_content = repaired_content
                        # Compress content
                        if b64_compressed:
                            try:
                                content_bytes = compress_from_b64(b64_compressed)
                            except ValueError:
                                content_bytes = compress(raw_content)
                        else:
                            content_bytes = compress(raw_content)

                if not error:
                    pm = PrivateMessage.objects.create(
                        sender=request.user,
                        recipient=recipient,
                        subject=subject,
                        content_compressed=content_bytes,
                    )
                    PrivateMessageBox.objects.create(
                        message=pm,
                        owner=request.user,
                        box_type=PrivateMessageBox.BoxType.OUTBOX,
                    )
                    return redirect("pm_outbox")

    return render(request, "board/pm_compose.html", {
        "error": error,
        "prefill_recipient": prefill_recipient,
        "prefill_subject": prefill_subject,
        "prefill_content": prefill_content,
        "pm_content_soft_limit": getattr(settings, "PM_CONTENT_SOFT_MAX_CHARS", 20_000),
    })


@login_required
def pm_edit(request, box_id):
    """Edit a message still in OUTBOX (not yet delivered)."""
    from .pm_utils import compress, compress_from_b64, decompress

    box = get_object_or_404(
        PrivateMessageBox.objects.select_related("message__recipient"),
        pk=box_id, owner=request.user, box_type=PrivateMessageBox.BoxType.OUTBOX,
    )
    pm = box.message
    current_content = decompress(pm.content_compressed)
    pm_content_limit = max(
        len(current_content),
        getattr(settings, "PM_CONTENT_SOFT_MAX_CHARS", 20_000),
    )

    # Double-check it's still undelivered
    if pm.delivered_at is not None:
        return redirect("pm_outbox")

    error = None
    if request.method == "POST":
        subject        = request.POST.get("subject", "").strip()
        raw_content    = request.POST.get("content", "")
        b64_compressed = request.POST.get("content_compressed", "").strip()

        if not subject or not raw_content:
            error = "Wypełnij wszystkie pola."
        else:
            repaired_content, changes, lint_errors = validate_pm_content(
                raw_content,
                original_size=len(current_content),
            )
            if lint_errors:
                error_lines = "\n".join(f"• {e}" for e in lint_errors)
                error = f"Błędy w kodzie BBCode:\n{error_lines}"
            else:
                raw_content = repaired_content
                if b64_compressed:
                    try:
                        content_bytes = compress_from_b64(b64_compressed)
                    except ValueError:
                        content_bytes = compress(raw_content)
                else:
                    content_bytes = compress(raw_content)
                pm.subject = subject
                pm.content_compressed = content_bytes
                pm.save(update_fields=["subject", "content_compressed"])
                return redirect("pm_outbox")

    return render(request, "board/pm_edit.html", {
        "box": box,
        "current_content": current_content,
        "error": error,
        "pm_content_soft_limit": pm_content_limit,
    })


@login_required
def pm_delete(request, box_id):
    """Delete a box entry. Deletes the PM itself if no other box entries remain."""
    if request.method != "POST":
        return HttpResponseForbidden()
    box = get_object_or_404(PrivateMessageBox, pk=box_id, owner=request.user)
    pm = box.message
    redirect_url = {
        "INBOX":  "pm_inbox",
        "OUTBOX": "pm_outbox",
        "SENT":   "pm_sent",
    }.get(box.box_type, "pm_inbox")
    box.delete()
    # If no box entries remain, delete the message itself
    if not pm.boxes.exists():
        pm.delete()
    return redirect(redirect_url)


@login_required
def toggle_post_like(request, post_id):
    if request.method != "POST":
        return HttpResponseForbidden()

    post = get_object_or_404(
        Post.objects.select_related("author", "topic"),
        pk=post_id,
    )
    next_url = request.POST.get("next") or f"/topic/{post.topic_id}/"
    scroll_to = (request.POST.get("scroll_to") or "").strip()
    if scroll_to.isdigit():
        joiner = "&" if "?" in next_url else "?"
        next_url = f"{next_url}{joiner}scroll_to={scroll_to}"

    if post.author_id == request.user.pk:
        messages.error(request, "Nie możesz polubić własnego posta.")
        return redirect(next_url)

    like = PostLike.objects.filter(post=post, user=request.user).first()
    if like is not None:
        like.delete()
        messages.success(request, "Wycofano polubienie.")
        return redirect(next_url)

    PostLike.objects.create(post=post, user=request.user)
    messages.success(request, "Dodano polubienie.")
    return redirect(next_url)


def user_likes_received(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    max_forum_level = getattr(request.user, "archive_access", 0) if request.user.is_authenticated else 0
    likes = (
        PostLike.objects
        .select_related("user", "post", "post__topic", "post__topic__forum")
        .filter(
            post__author=target_user,
            post__topic__forum__archive_level__lte=max_forum_level,
        )
        .order_by("-created_at", "-pk")
    )
    page = Paginator(likes, getattr(settings, "POSTS_PER_PAGE", 20)).get_page(request.GET.get("page"))
    return render(request, "board/user_likes.html", {
        "target_user": target_user,
        "page": page,
        "mode": "received",
    })


def user_likes_given(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    max_forum_level = getattr(request.user, "archive_access", 0) if request.user.is_authenticated else 0
    likes = (
        PostLike.objects
        .select_related("post", "post__author", "post__topic", "post__topic__forum")
        .filter(
            user=target_user,
            post__topic__forum__archive_level__lte=max_forum_level,
        )
        .order_by("-created_at", "-pk")
    )
    page = Paginator(likes, getattr(settings, "POSTS_PER_PAGE", 20)).get_page(request.GET.get("page"))
    return render(request, "board/user_likes.html", {
        "target_user": target_user,
        "page": page,
        "mode": "given",
    })


# ---------------------------------------------------------------------------
# Quote link: redirect to the source post
# ---------------------------------------------------------------------------


def _assign_checklist_anon_labels(temp_users_qs):
    """Assign 'Anonimus_N' labels to checklist items/comments by temp users.

    Within each checklist, the same user gets the same number.
    Must be called BEFORE deleting temp users (SET_NULL will clear author FK).
    """

    temp_user_ids = set(temp_users_qs.values_list("pk", flat=True))
    if not temp_user_ids:
        return

    # Gather all checklists that have contributions from temp users
    item_checklist_ids = set(
        ChecklistItem.objects.filter(author_id__in=temp_user_ids)
        .values_list("checklist_id", flat=True).distinct()
    )
    comment_checklist_ids = set(
        ChecklistComment.objects.filter(author_id__in=temp_user_ids)
        .values_list("item__checklist_id", flat=True).distinct()
    )
    checklist_ids = item_checklist_ids | comment_checklist_ids

    for cl_id in checklist_ids:
        # Collect unique temp user IDs in this checklist, ordered by first appearance
        user_ids_items = list(
            ChecklistItem.objects.filter(checklist_id=cl_id, author_id__in=temp_user_ids)
            .order_by("created_at")
            .values_list("author_id", flat=True)
        )
        user_ids_comments = list(
            ChecklistComment.objects.filter(item__checklist_id=cl_id, author_id__in=temp_user_ids)
            .order_by("created_at")
            .values_list("author_id", flat=True)
        )
        seen = {}
        counter = 0
        for uid in user_ids_items + user_ids_comments:
            if uid not in seen:
                counter += 1
                seen[uid] = f"Anonimus_{counter}"

        # Update labels
        for uid, label in seen.items():
            ChecklistItem.objects.filter(
                checklist_id=cl_id, author_id=uid
            ).update(author_label=label)
            ChecklistComment.objects.filter(
                item__checklist_id=cl_id, author_id=uid
            ).update(author_label=label)


def _preserve_checklist_anon_upvotes(temp_users_qs):
    """Before deleting temp users, add their upvote counts to anon_upvote_count."""
    from django.db.models import Count

    temp_user_ids = set(temp_users_qs.values_list("pk", flat=True))
    if not temp_user_ids:
        return

    anon_counts = (
        ChecklistUpvote.objects.filter(user_id__in=temp_user_ids)
        .values("item_id")
        .annotate(cnt=Count("pk"))
    )
    for row in anon_counts:
        ChecklistItem.objects.filter(pk=row["item_id"]).update(
            anon_upvote_count=django_models.F("anon_upvote_count") + row["cnt"]
        )


def _cleanup_temporary():
    """Delete temporary users, posts, and topics.  Returns stats dict."""
    from .models import Forum as _Forum

    # Topics with at least one permanent post survive
    surviving_topic_ids = set(
        Post.objects.filter(is_temporary=False)
        .values_list("topic_id", flat=True)
        .distinct()
    )

    # Gather affected forum IDs for stat recalculation
    affected_forum_ids = set(
        Topic.objects.filter(is_temporary=True)
        .values_list("forum_id", flat=True)
        .distinct()
    )

    # Delete temporary posts
    _, _detail = Post.objects.filter(is_temporary=True).delete()
    del_posts = _detail.get("board.Post", 0)

    # Mark surviving topics as permanent
    Topic.objects.filter(pk__in=surviving_topic_ids, is_temporary=True).update(is_temporary=False)

    # Delete topics that have no permanent posts left
    _, _detail = Topic.objects.filter(is_temporary=True).delete()
    del_topics = _detail.get("board.Topic", 0)

    # Collect poll options affected by temporary user votes before deletion
    from django.db.models import Count
    affected_option_ids = set(
        PollOption.objects.filter(
            votes__user__is_temporary=True
        ).values_list("pk", flat=True).distinct()
    )

    # Checklist: assign Anonimus_N labels and preserve upvote counts
    _assign_checklist_anon_labels(User.objects.filter(is_temporary=True))
    _preserve_checklist_anon_upvotes(User.objects.filter(is_temporary=True))

    # Delete temporary users (cascades PollVotes and ChecklistUpvotes)
    _, _detail = User.objects.filter(is_temporary=True).delete()
    del_users = _detail.get("board.User", 0)

    # Recalculate vote_count for poll options that lost votes
    if affected_option_ids:
        for opt in PollOption.objects.filter(pk__in=affected_option_ids).annotate(
            real_count=Count("votes")
        ):
            if opt.vote_count != opt.real_count:
                opt.vote_count = opt.real_count
                opt.save(update_fields=["vote_count"])

    # Recalculate checklist upvote_count from remaining records
    for item in ChecklistItem.objects.annotate(
        real_count=Count("upvotes")
    ).exclude(upvote_count=django_models.F("real_count")):
        item.upvote_count = item.real_count
        item.save(update_fields=["upvote_count"])

    # Recalculate forum/topic/user stats
    for forum in _Forum.objects.filter(pk__in=affected_forum_ids):
        forum.topic_count = forum.topics.count()
        forum.post_count = Post.objects.filter(topic__forum=forum).count()
        last = (
            Post.objects.filter(topic__forum=forum)
            .order_by("-created_at")
            .first()
        )
        forum.last_post = last
        forum.last_post_at = last.created_at if last else None
        forum.save(update_fields=["topic_count", "post_count", "last_post", "last_post_at"])

    for topic in Topic.objects.filter(forum_id__in=affected_forum_ids):
        topic.reply_count = max(0, topic.posts.count() - 1)
        last = topic.posts.order_by("-created_at").first()
        topic.last_post = last
        topic.last_post_at = last.created_at if last else None
        topic.save(update_fields=["reply_count", "last_post", "last_post_at"])

    # Recalculate post_count for all users (simpler than tracking affected ones)
    from django.db.models import Count
    for u in User.objects.annotate(real_count=Count("posts")).exclude(is_root=True):
        if u.post_count != u.real_count:
            u.post_count = u.real_count
            u.save(update_fields=["post_count"])

    return {"users": del_users, "posts": del_posts, "topics": del_topics}


def root_config(request):
    """Root-only view to toggle site-wide settings."""
    from .models import SiteConfig, MaintenanceAllowedUser
    from django.core.exceptions import ValidationError
    if not request.user.is_authenticated or not request.user.is_root:
        return HttpResponseForbidden()

    cfg = SiteConfig.get()
    all_users = User.objects.order_by("username")
    empty_users = User.objects.filter(
        is_root=False,
        posts__isnull=True,
        topics__isnull=True,
        pm_boxes__isnull=True,
        sent_pms__isnull=True,
        received_pms__isnull=True,
    ).distinct().order_by("username")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "flush_reset_codes":
            PasswordResetCode.objects.all().delete()
        elif action == "add_maintenance_user":
            username = request.POST.get("maint_username", "").strip()
            if not username:
                messages.error(request, "Podaj nick.")
            elif not User.objects.filter(username=username).exists():
                messages.error(request, f"Użytkownik '{username}' nie istnieje.")
            else:
                _, created = MaintenanceAllowedUser.objects.get_or_create(username=username)
                if created:
                    messages.success(request, f"Dodano '{username}' do listy serwisowej.")
                else:
                    messages.warning(request, f"'{username}' już jest na liście.")
        elif action == "remove_maintenance_user":
            username = request.POST.get("maint_username", "").strip()
            if username == "root":
                messages.error(request, "Nie można usunąć roota z listy serwisowej.")
            elif username:
                deleted, _ = MaintenanceAllowedUser.objects.filter(username=username).delete()
                if deleted:
                    messages.success(request, f"Usunięto '{username}' z listy serwisowej.")
        elif action == "cleanup_temporary":
            stats = _cleanup_temporary()
            messages.success(
                request,
                f"Wyczyszczono: {stats['users']} kont, {stats['posts']} postów, {stats['topics']} wątków.",
            )
        elif action == "rename_user":
            user_id = request.POST.get("rename_user_id", "")
            new_username = request.POST.get("new_username", "")
            try:
                target_user = all_users.get(pk=int(user_id))
                result = rename_user_and_update_quotes(target_user, new_username)
            except (ValueError, User.DoesNotExist):
                messages.error(request, "Nie wybrano poprawnego użytkownika.")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(
                    request,
                    f"Zmieniono nick '{result['old_username']}' → "
                    f"'{result['new_username']}'. Poprawiono {result['tags_changed']} "
                    f"tagów quote w {result['posts_changed']} postach."
                )
        elif action == "delete_empty_users":
            selected_ids = [
                int(user_id) for user_id in request.POST.getlist("user_ids") if user_id.isdigit()
            ]
            if selected_ids:
                deletable = empty_users.filter(id__in=selected_ids)
                deleted_count = deletable.count()
                deletable.delete()
                skipped = len(selected_ids) - deleted_count
                if deleted_count:
                    messages.success(
                        request,
                        f"Usunięto {deleted_count} pustych kont. Pominięto {skipped}."
                    )
                else:
                    messages.warning(request, "Nie usunięto żadnego konta.")
            else:
                messages.warning(request, "Nie zaznaczono żadnego konta.")
        else:
            old_mode = cfg.site_mode
            new_mode = request.POST.get("site_mode", SiteConfig.MODE_PRODUCTION)
            if new_mode in (SiteConfig.MODE_PRODUCTION, SiteConfig.MODE_READONLY, SiteConfig.MODE_MAINTENANCE, SiteConfig.MODE_BETA):
                cfg.site_mode = new_mode
            # Auto-cleanup when leaving maintenance/beta
            _temp_modes = (SiteConfig.MODE_MAINTENANCE, SiteConfig.MODE_BETA)
            if old_mode in _temp_modes and new_mode not in _temp_modes:
                stats = _cleanup_temporary()
                if stats["users"] or stats["posts"] or stats["topics"]:
                    messages.info(
                        request,
                        f"Automatyczne czyszczenie: {stats['users']} kont, "
                        f"{stats['posts']} postów, {stats['topics']} wątków.",
                    )
            cfg.maintenance_message = request.POST.get("maintenance_message", "").strip()
            cfg.reg_ip_limit = "reg_ip_limit" in request.POST
            hard_limit = getattr(settings, "POLL_OPTIONS_HARD_MAX", 64)
            try:
                cfg.search_snippet_chars = max(
                    80,
                    int(request.POST.get("search_snippet_chars", cfg.search_snippet_chars)),
                )
                cfg.poll_options_soft_max = min(
                    hard_limit,
                    max(2, int(request.POST.get("poll_options_soft_max", cfg.poll_options_soft_max))),
                )
                cfg.reg_ip_window_hours = max(1, int(request.POST.get("reg_ip_window_hours", cfg.reg_ip_window_hours)))
                cfg.reg_ip_max_real = max(0, int(request.POST.get("reg_ip_max_real", cfg.reg_ip_max_real)))
                cfg.reg_ip_max_temp = max(0, int(request.POST.get("reg_ip_max_temp", cfg.reg_ip_max_temp)))
                cfg.pm_min_active_days = max(0, int(request.POST.get("pm_min_active_days", cfg.pm_min_active_days)))
                cfg.pm_max_burst = max(1, int(request.POST.get("pm_max_burst", cfg.pm_max_burst)))
                cfg.pm_cold_reset_hours = max(1, int(request.POST.get("pm_cold_reset_hours", cfg.pm_cold_reset_hours)))
                cfg.pm_new_recipients_per_day = max(1, int(request.POST.get("pm_new_recipients_per_day", cfg.pm_new_recipients_per_day)))
            except (TypeError, ValueError):
                messages.error(request, "Wartości liczbowe są nieprawidłowe.")
                return redirect("root_config")
            cfg.save()
        return redirect("root_config")

    temp_users = User.objects.filter(is_temporary=True).count()
    temp_posts = Post.objects.filter(is_temporary=True).count()
    temp_topics = Topic.objects.filter(is_temporary=True).count()

    return render(request, "board/root_config.html", {
        "cfg": cfg,
        "SiteConfig": SiteConfig,
        "reset_codes_count": PasswordResetCode.objects.count(),
        "all_users": all_users,
        "empty_users": empty_users,
        "maintenance_users": MaintenanceAllowedUser.objects.order_by("username"),
        "temp_users": temp_users,
        "temp_posts": temp_posts,
        "temp_topics": temp_topics,
    })


def goto_post(request, post_id):
    """Redirect to the topic page anchored at the given post."""
    from django.http import Http404
    try:
        post = Post.objects.select_related("topic").get(pk=post_id)
    except Post.DoesNotExist:
        raise Http404
    url = f"/topic/{post.topic_id}/?page={_post_page(post)}#post-{post_id}"
    from django.http import HttpResponseRedirect
    return HttpResponseRedirect(url)


# ---------------------------------------------------------------------------
# Admin: forum/section order management (root only)
# ---------------------------------------------------------------------------

def _swap_order(model_class, pk, direction, filter_kwargs):
    """Swap 'order' of item pk with the adjacent item (up/down) in the given queryset."""
    items = list(model_class.objects.filter(**filter_kwargs).order_by("order"))
    idx = next((i for i, x in enumerate(items) if x.pk == pk), None)
    if idx is None:
        return
    if direction == "up" and idx > 0:
        other_idx = idx - 1
    elif direction == "down" and idx < len(items) - 1:
        other_idx = idx + 1
    else:
        return
    a, b = items[idx], items[other_idx]
    a.order, b.order = b.order, a.order
    model_class.objects.bulk_update([a, b], ["order"])


@login_required
def admin_order(request):
    if not request.user.is_root:
        return HttpResponseForbidden()
    sections = Section.objects.order_by("order")
    top_forums = Forum.objects.filter(parent__isnull=True).order_by("order").select_related("section")
    return render(request, "board/admin_order.html", {
        "sections": sections,
        "top_forums": top_forums,
        "parent_forum": None,
    })


@login_required
def admin_order_children(request, forum_id):
    if not request.user.is_root:
        return HttpResponseForbidden()
    parent = get_object_or_404(Forum, pk=forum_id)
    children = Forum.objects.filter(parent=parent).order_by("order")
    return render(request, "board/admin_order.html", {
        "sections": None,
        "top_forums": children,
        "parent_forum": parent,
    })


@login_required
def admin_order_move_section(request, pk, direction):
    if not request.user.is_root:
        return HttpResponseForbidden()
    if request.method != "POST":
        return HttpResponseForbidden()
    _swap_order(Section, pk, direction, {})
    return redirect("admin_order")


@login_required
def admin_order_move_forum(request, pk, direction):
    if not request.user.is_root:
        return HttpResponseForbidden()
    if request.method != "POST":
        return HttpResponseForbidden()
    forum = get_object_or_404(Forum, pk=pk)
    parent_id = forum.parent_id
    _swap_order(Forum, pk, direction, {"parent_id": parent_id})
    # redirect back to the right page
    if parent_id:
        return redirect("admin_order_children", forum_id=parent_id)
    return redirect("admin_order")


# ---------------------------------------------------------------------------
# Role management
# ---------------------------------------------------------------------------

def _role_actor_check(user):
    """Return (is_root, is_admin) or raise HttpResponseForbidden."""
    if not user.is_authenticated:
        return None, None
    return user.is_root, (user.role == User.ROLE_ADMIN)


@login_required
def set_role(request):
    """POST-only endpoint to change a user's role.
    Root can set 0/1/2. Admin (role=2) can set 0/1 for non-admins only.
    """
    is_root = request.user.is_root
    is_admin = request.user.role == User.ROLE_ADMIN
    if not is_root and not is_admin:
        return HttpResponseForbidden()
    if request.method != "POST":
        return HttpResponseForbidden()

    next_url = request.POST.get("next") or ("root_config" if is_root else "admin_roles")
    try:
        target = User.objects.get(pk=int(request.POST["user_id"]))
        new_role = int(request.POST["role"])
    except (KeyError, ValueError, User.DoesNotExist):
        messages.error(request, "Nieprawidłowy użytkownik lub rola.")
        return redirect(next_url)

    if target.is_root:
        messages.error(request, "Nie można zmieniać roli roota.")
        return redirect(next_url)

    if new_role not in (User.ROLE_USER, User.ROLE_MODERATOR, User.ROLE_ADMIN):
        messages.error(request, "Nieprawidłowa rola.")
        return redirect(next_url)

    if is_admin and not is_root:
        if new_role == User.ROLE_ADMIN:
            messages.error(request, "Administratorzy nie mogą nadawać uprawnień administratora.")
            return redirect(next_url)
        if target.role == User.ROLE_ADMIN:
            messages.error(request, "Administratorzy nie mogą zmieniać roli innych administratorów.")
            return redirect(next_url)

    role_labels = dict(User.ROLE_CHOICES)
    old_label = role_labels.get(target.role, target.role)
    new_label = role_labels.get(new_role, new_role)
    target.role = new_role
    target.save(update_fields=["role"])
    messages.success(request, f"'{target.username}': {old_label} → {new_label}.")
    return redirect(next_url)


@login_required
def admin_roles(request):
    """Admin view for managing moderator assignments."""
    is_root = request.user.is_root
    is_admin = request.user.role == User.ROLE_ADMIN
    if not is_root and not is_admin:
        return HttpResponseForbidden()

    users = (
        User.objects.filter(is_root=False)
        .order_by("-role", "username")
        .only("id", "username", "role")
    )
    return render(request, "board/admin_roles.html", {
        "users": users,
        "is_root": is_root,
        "Role": User,
    })


# ---------------------------------------------------------------------------
# Moderator topic controls: sticky / announcement / lock
# ---------------------------------------------------------------------------

@login_required
def set_topic_type(request, topic_id):
    """POST: set topic_type to NORMAL/STICKY/ANNOUNCEMENT.
    Moderators can set NORMAL or STICKY.
    Admins and root can also set ANNOUNCEMENT.
    """
    topic = get_object_or_404(Topic, pk=topic_id)
    if not _is_moderator(request.user, topic.forum):
        return HttpResponseForbidden()
    if request.method != "POST":
        return HttpResponseForbidden()

    try:
        new_type = int(request.POST["topic_type"])
    except (KeyError, ValueError):
        messages.error(request, "Nieprawidłowy typ wątku.")
        return redirect("topic_detail", topic_id=topic_id)

    allowed = {Topic.TopicType.NORMAL, Topic.TopicType.STICKY}
    if request.user.is_root or request.user.role >= User.ROLE_ADMIN:
        allowed.add(Topic.TopicType.ANNOUNCEMENT)

    if new_type not in allowed:
        messages.error(request, "Brak uprawnień do ustawienia tego typu wątku.")
        return redirect("topic_detail", topic_id=topic_id)

    topic.topic_type = new_type
    topic.save(update_fields=["topic_type"])
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def lock_topic(request, topic_id):
    """POST: toggle is_locked on a topic. Moderators and above."""
    topic = get_object_or_404(Topic, pk=topic_id)
    if not _is_moderator(request.user, topic.forum):
        return HttpResponseForbidden()
    if request.method != "POST":
        return HttpResponseForbidden()

    topic.is_locked = not topic.is_locked
    topic.save(update_fields=["is_locked"])
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def convert_post_permanent(request, post_id):
    """Convert a temporary post to permanent (admin/root only)."""
    if request.method != "POST":
        return HttpResponseForbidden()
    if not (request.user.is_root or request.user.role >= User.ROLE_ADMIN):
        return HttpResponseForbidden()

    post = get_object_or_404(Post.objects.select_related("author", "topic"), pk=post_id)
    ok, reason = can_convert_to_permanent(post)
    if not ok:
        reason_map = {
            "not_temporary": "Ten post nie jest tymczasowy.",
            "temporary_user": "Post tymczasowego użytkownika nie może być zamieniony na trwały.",
            "quotes_temporary": "Post cytuje tymczasowe posty — nie może być zamieniony na trwały.",
            "feature_first_post": "W wątku z ankietą/checklistą najpierw należy oznaczyć pierwszy post jako trwały.",
        }
        messages.error(request, reason_map.get(reason, "Nie można zamienić."))
    else:
        post.is_temporary = False
        post.save(update_fields=["is_temporary"])
        # If topic now has a permanent post, mark it permanent too
        topic = post.topic
        if topic.is_temporary:
            topic.is_temporary = False
            topic.save(update_fields=["is_temporary"])
        messages.success(request, f"Post #{post.post_order} zamieniony na trwały.")

    return redirect("topic_detail", topic_id=post.topic_id)


def logout_view(request):
    """Logout that preserves maintenance_access so gate users stay on the forum."""
    from django.contrib.auth import logout as auth_logout
    # Save gate flags before flush
    maintenance_access = request.session.get("maintenance_access")
    maintenance_user = request.session.get("maintenance_user")
    auth_logout(request)  # flushes entire session
    if maintenance_access:
        request.session["maintenance_access"] = maintenance_access
        request.session["maintenance_user"] = maintenance_user
    return redirect("login")


def maintenance_gate(request):
    """Stage-1 gate for closed maintenance mode.

    Verifies nick+password (NO TOR check) and checks the MaintenanceAllowedUser
    list (or staff).  On success sets session['maintenance_access'] = True and
    logs out any existing forum session — so forum login remains a separate step.
    """
    from django.contrib.auth import authenticate, logout as auth_logout
    from .models import SiteConfig, MaintenanceAllowedUser

    # Already past the gate
    if request.session.get("maintenance_access"):
        return redirect("/")

    cfg = SiteConfig.get()
    message = cfg.maintenance_message or "Trwa przerwa techniczna."
    error = None

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)
        if user is None:
            error = "Nieprawidłowy nick lub hasło."
        elif not (getattr(user, "is_root", False) or MaintenanceAllowedUser.objects.filter(username=user.username).exists()):
            error = "Ten nick nie jest na liście serwisowej."
        else:
            # Ensure root always has a visible DB entry (lazy creation)
            if getattr(user, "is_root", False):
                MaintenanceAllowedUser.objects.get_or_create(username=user.username)
            gate_username = user.username
            # Log out any existing forum session so login stays a separate step
            auth_logout(request)
            request.session["maintenance_access"] = True
            request.session["maintenance_user"] = gate_username
            return redirect("/")

    return render(request, "board/maintenance_gate.html", {
        "message": message,
        "error": error,
    })


def maintenance_logout(request):
    """Clear maintenance_access session flag and log out from forum."""
    from django.contrib.auth import logout as auth_logout
    request.session.pop("maintenance_access", None)
    request.session.pop("maintenance_user", None)
    auth_logout(request)
    return redirect("maintenance_gate")


# ---------------------------------------------------------------------------
# Checklist views
# ---------------------------------------------------------------------------

def _get_checklist_context(request, topic_id):
    """Return (topic, checklist, is_owner_or_mod) or raise 404."""
    from django.http import Http404
    topic = get_object_or_404(Topic.objects.select_related("author"), pk=topic_id)
    try:
        checklist = topic.checklist
    except Checklist.DoesNotExist:
        raise Http404
    is_owner = request.user.is_authenticated and topic.author_id == request.user.pk
    is_mod = request.user.is_authenticated and (
        request.user.is_root or request.user.role >= User.ROLE_ADMIN
    )
    return topic, checklist, (is_owner or is_mod)


def _get_checklist_item(checklist, item_id):
    return get_object_or_404(ChecklistItem, pk=item_id, checklist=checklist)


@login_required
def checklist_add_item(request, topic_id):
    """Add a new checklist item. PENDING for regular users, NEW for owner/mod."""
    from django.http import JsonResponse
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if checklist.is_closed and not is_owner_or_mod:
        messages.error(request, "Checklista jest zamknięta.")
        return redirect("topic_detail", topic_id=topic_id)
    if not checklist.allow_user_proposals and not is_owner_or_mod:
        messages.error(request, "Dodawanie pozycji jest wyłączone.")
        return redirect("topic_detail", topic_id=topic_id)
    if request.method != "POST":
        return redirect("topic_detail", topic_id=topic_id)

    from .antiflood import check_can_post as _flood_check
    flood = _flood_check(request.user)
    if not flood["allowed"]:
        messages.error(request, str(flood["wait_seconds"]), extra_tags="antiflood")
        return redirect("topic_detail", topic_id=topic_id)

    from .forms import ChecklistItemForm
    form = ChecklistItemForm(request.POST)
    if not form.is_valid():
        for err in form.errors.values():
            messages.error(request, err[0])
        return redirect("topic_detail", topic_id=topic_id)

    category = None
    cat_id = form.cleaned_data.get("category")
    if cat_id:
        category = checklist.categories.filter(pk=cat_id).first()

    allowed_tags = [t.strip() for t in checklist.allowed_tags.split(",") if t.strip()]
    raw_tag = request.POST.get("tag", "").strip()
    tag = raw_tag if raw_tag in allowed_tags else ""

    status = ChecklistItem.Status.NEW if is_owner_or_mod else ChecklistItem.Status.PENDING
    max_order = checklist.items.aggregate(m=django_models.Max("order"))["m"] or 0

    item = ChecklistItem.objects.create(
        checklist=checklist,
        author=request.user,
        title=form.cleaned_data["title"],
        description=form.cleaned_data.get("description", ""),
        category=category,
        tag=tag,
        status=status,
        order=max_order + 1,
    )
    # Auto-upvote own item
    ChecklistUpvote.objects.create(item=item, user=request.user)
    item.upvote_count = 1
    item.save(update_fields=["upvote_count"])

    if status == ChecklistItem.Status.PENDING:
        messages.info(request, "Propozycja dodana — czeka na zatwierdzenie.")
    else:
        messages.success(request, "Pozycja dodana.")
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def checklist_approve_item(request, topic_id, item_id):
    if request.method != "POST":
        return HttpResponseForbidden()
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()
    item = _get_checklist_item(checklist, item_id)
    if item.status != ChecklistItem.Status.PENDING:
        messages.error(request, "Ta pozycja nie czeka na zatwierdzenie.")
        return redirect("topic_detail", topic_id=topic_id)
    item.status = ChecklistItem.Status.NEW
    item.status_changed_at = timezone.now()
    item.status_changed_by = request.user
    item.save(update_fields=["status", "status_changed_at", "status_changed_by"])
    messages.success(request, f"Zatwierdzono: {item.title}")
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def checklist_reject_item(request, topic_id, item_id):
    if request.method != "POST":
        return HttpResponseForbidden()
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()
    item = _get_checklist_item(checklist, item_id)
    if item.status != ChecklistItem.Status.PENDING:
        messages.error(request, "Ta pozycja nie czeka na zatwierdzenie.")
        return redirect("topic_detail", topic_id=topic_id)
    item.status = ChecklistItem.Status.REJECTED
    item.rejection_reason = request.POST.get("reason", "")[:500]
    item.status_changed_at = timezone.now()
    item.status_changed_by = request.user
    item.save(update_fields=["status", "rejection_reason", "status_changed_at", "status_changed_by"])
    messages.success(request, f"Odrzucono: {item.title}")
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def checklist_set_status(request, topic_id, item_id):
    if request.method != "POST":
        return HttpResponseForbidden()
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()
    item = _get_checklist_item(checklist, item_id)
    try:
        new_status = int(request.POST.get("status", ""))
    except (ValueError, TypeError):
        return HttpResponseForbidden()
    if new_status not in dict(ChecklistItem.Status.choices):
        return HttpResponseForbidden()
    item.status = new_status
    item.status_changed_at = timezone.now()
    item.status_changed_by = request.user
    item.save(update_fields=["status", "status_changed_at", "status_changed_by"])
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def checklist_set_priority(request, topic_id, item_id):
    if request.method != "POST":
        return HttpResponseForbidden()
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()
    item = _get_checklist_item(checklist, item_id)
    raw = request.POST.get("priority", "")
    if raw == "":
        item.priority = None
    else:
        try:
            p = int(raw)
        except (ValueError, TypeError):
            return HttpResponseForbidden()
        if p not in dict(ChecklistItem.Priority.choices):
            return HttpResponseForbidden()
        item.priority = p
    item.save(update_fields=["priority"])
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def checklist_delete_item(request, topic_id, item_id):
    if request.method != "POST":
        return HttpResponseForbidden()
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()
    item = _get_checklist_item(checklist, item_id)
    item.delete()
    messages.success(request, "Pozycja usunięta.")
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def checklist_toggle_upvote(request, topic_id, item_id):
    """AJAX: toggle upvote on a checklist item."""
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    topic, checklist, _ = _get_checklist_context(request, topic_id)
    if checklist.is_closed:
        return JsonResponse({"error": "Checklista zamknięta."}, status=403)
    item = _get_checklist_item(checklist, item_id)
    existing = ChecklistUpvote.objects.filter(item=item, user=request.user).first()
    if existing:
        existing.delete()
        upvoted = False
    else:
        ChecklistUpvote.objects.create(item=item, user=request.user)
        upvoted = True
    item.upvote_count = item.upvotes.count()
    item.save(update_fields=["upvote_count"])
    return JsonResponse({
        "ok": True,
        "upvoted": upvoted,
        "count": item.total_upvotes,
    })


@login_required
def checklist_item_comments(request, topic_id, item_id):
    """AJAX: load comments for a checklist item."""
    from django.http import JsonResponse
    topic, checklist, _ = _get_checklist_context(request, topic_id)
    item = _get_checklist_item(checklist, item_id)
    comments = item.comments.select_related("author").order_by("created_at")
    data = []
    for c in comments:
        data.append({
            "id": c.pk,
            "author": c.display_author(),
            "author_id": c.author_id,
            "content": c.content,
            "created_at": c.created_at.strftime("%Y-%m-%d %H:%M"),
        })
    return JsonResponse({"comments": data})


@login_required
def checklist_add_comment(request, topic_id, item_id):
    """AJAX: add a comment to a checklist item."""
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    topic, checklist, _ = _get_checklist_context(request, topic_id)
    if checklist.is_closed:
        return JsonResponse({"error": "Checklista zamknięta."}, status=403)
    item = _get_checklist_item(checklist, item_id)

    import json
    try:
        body = json.loads(request.body)
        content = body.get("content", "").strip()
    except (json.JSONDecodeError, AttributeError):
        content = request.POST.get("content", "").strip()

    if not content:
        return JsonResponse({"error": "Treść komentarza jest wymagana."}, status=400)
    if len(content) > 1000:
        return JsonResponse({"error": "Komentarz zbyt długi (max 1000 znaków)."}, status=400)

    comment = ChecklistComment.objects.create(
        item=item,
        author=request.user,
        content=content,
    )
    item.comment_count = item.comments.count()
    item.save(update_fields=["comment_count"])

    return JsonResponse({
        "ok": True,
        "comment": {
            "id": comment.pk,
            "author": comment.display_author(),
            "author_id": comment.author_id,
            "content": comment.content,
            "created_at": comment.created_at.strftime("%Y-%m-%d %H:%M"),
        },
    })


@login_required
def checklist_manage_categories(request, topic_id):
    """Full page: manage checklist categories."""
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()

    from .forms import ChecklistCategoryForm

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "add":
            form = ChecklistCategoryForm(request.POST)
            if form.is_valid():
                max_order = checklist.categories.aggregate(m=django_models.Max("order"))["m"] or 0
                ChecklistCategory.objects.create(
                    checklist=checklist,
                    name=form.cleaned_data["name"],
                    color=form.cleaned_data["color"],
                    order=max_order + 1,
                )
                messages.success(request, "Kategoria dodana.")
        elif action == "delete":
            cat_id = request.POST.get("category_id")
            checklist.categories.filter(pk=cat_id).delete()
            messages.success(request, "Kategoria usunięta.")
        elif action == "edit":
            cat_id = request.POST.get("category_id")
            cat = checklist.categories.filter(pk=cat_id).first()
            if cat:
                form = ChecklistCategoryForm(request.POST)
                if form.is_valid():
                    cat.name = form.cleaned_data["name"]
                    cat.color = form.cleaned_data["color"]
                    cat.save(update_fields=["name", "color"])
                    messages.success(request, "Kategoria zaktualizowana.")
        return redirect("checklist_manage_categories", topic_id=topic_id)

    categories = checklist.categories.all()
    return render(request, "board/checklist_categories.html", {
        "topic": topic,
        "checklist": checklist,
        "categories": categories,
        "form": ChecklistCategoryForm(),
    })


@login_required
def checklist_toggle_closed(request, topic_id):
    if request.method != "POST":
        return HttpResponseForbidden()
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()
    if checklist.is_closed:
        checklist.is_closed = False
        checklist.closed_at = None
        messages.success(request, "Checklista otwarta ponownie.")
    else:
        checklist.is_closed = True
        checklist.closed_at = timezone.now()
        messages.success(request, "Checklista zamknięta.")
    checklist.save(update_fields=["is_closed", "closed_at"])
    return redirect("topic_detail", topic_id=topic_id)


@login_required
def checklist_reorder(request, topic_id):
    """AJAX: reorder checklist items via drag-and-drop."""
    from django.http import JsonResponse
    import json
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return JsonResponse({"error": "Brak uprawnień."}, status=403)
    try:
        body = json.loads(request.body)
        order_ids = body.get("order", [])
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({"error": "Nieprawidłowe dane."}, status=400)

    items = {item.pk: item for item in checklist.items.all()}
    updates = []
    for idx, item_id in enumerate(order_ids):
        item = items.get(item_id)
        if item and item.order != idx:
            item.order = idx
            updates.append(item)
    if updates:
        ChecklistItem.objects.bulk_update(updates, ["order"])
    return JsonResponse({"ok": True})


@login_required
def checklist_settings(request, topic_id):
    if request.method != "POST":
        return HttpResponseForbidden()
    topic, checklist, is_owner_or_mod = _get_checklist_context(request, topic_id)
    if not is_owner_or_mod:
        return HttpResponseForbidden()
    checklist.allow_user_proposals = request.POST.get("allow_user_proposals") == "1"
    default_sort = request.POST.get("default_sort", "upvotes")
    if default_sort in dict(Checklist.DefaultSort.choices):
        checklist.default_sort = default_sort
    raw_tags = request.POST.get("allowed_tags", "")
    parsed = [t.strip() for t in raw_tags.split(",") if t.strip()]
    checklist.allowed_tags = ", ".join(parsed)
    checklist.save(update_fields=["allow_user_proposals", "default_sort", "allowed_tags"])
    messages.success(request, "Ustawienia zapisane.")
    return redirect("topic_detail", topic_id=topic_id)
