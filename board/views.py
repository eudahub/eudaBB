import secrets
import re
from html import escape
from datetime import timedelta

from django.db import models as django_models
from django.db import transaction
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.utils import timezone
from django.conf import settings

from .models import Section, Forum, Topic, Post, User, ActivationToken, BlockedIP, PasswordResetCode, PrivateMessage, PrivateMessageBox, PostLike, PostSearchIndex, SiteConfig, Poll, PollOption, PollVote
from .forms import (
    RegisterForm, RegisterStartForm, RegisterFinishForm,
    NewTopicForm, ReplyForm, validate_post_content, validate_pm_content,
)
from .email_utils import mask_email, mask_email_variants
from .spam_utils import get_author_spam_filter, filter_forums
from .middleware import invalidate_blocked_ips_cache
from .auth_utils import prehash_password
from .username_utils import normalize
from .user_rename import rename_user_and_update_quotes
from .quote_refs import rebuild_quote_references_for_post
from .quote_selection import extract_exact_quote_fragment, normalize_selected_text
from .search_index import extract_author_search_text, normalize_search_text, strip_diacritics


# ---------------------------------------------------------------------------
# Stat helpers — keep view functions small
# ---------------------------------------------------------------------------

def _update_topic_stats(topic: Topic, last_post: Post) -> None:
    """Recalculate and save cached counters on a topic after a new post."""
    topic.reply_count = topic.posts.count() - 1  # first post is not a "reply"
    topic.last_post = last_post
    topic.last_post_at = last_post.created_at
    topic.save(update_fields=["reply_count", "last_post", "last_post_at"])


def _update_forum_stats(forum: Forum, last_post: Post) -> None:
    """Recalculate and save cached counters on a forum after a new post."""
    forum.post_count = Post.objects.filter(topic__forum=forum).count()
    forum.topic_count = forum.topics.count()
    forum.last_post = last_post
    forum.last_post_at = last_post.created_at
    forum.save(update_fields=["post_count", "topic_count", "last_post", "last_post_at"])


def _increment_user_post_count(user) -> None:
    """Increment post counter on user model."""
    if user and user.is_authenticated:
        user.post_count += 1
        user.save(update_fields=["post_count"])


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
                             post_order: int, author_ip: str = None) -> Post:
    retain_until = _retain_until(flagged=False) if author_ip else None
    post = Post.objects.create(
        topic=topic,
        author=author,
        content_bbcode=content_bbcode,
        post_order=post_order,
        author_ip=author_ip,
        ip_retain_until=retain_until,
    )
    rebuild_quote_references_for_post(post)
    return post


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
    topics_qs = (
        forum.topics
        .select_related("author", "last_post", "last_post__author")
        .filter(get_author_spam_filter(request.user))
    )
    paginator = Paginator(topics_qs, getattr(settings, "TOPICS_PER_PAGE", 30))
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "board/forum_detail.html", {"forum": forum, "page": page})


def topic_detail(request, topic_id):
    """Post list for a single topic, paginated. Increments view counter."""
    topic = get_object_or_404(
        Topic.objects.select_related("poll").prefetch_related("poll__options"),
        pk=topic_id,
    )

    # Increment view counter (simple version — no dedup)
    Topic.objects.filter(pk=topic_id).update(view_count=topic.view_count + 1)

    # Paginacja stabilna — wszystkie posty, niezależnie od PLONK
    posts_qs = topic.posts.select_related("author", "updated_by")
    paginator = Paginator(posts_qs, getattr(settings, "POSTS_PER_PAGE", 20))
    page = paginator.get_page(request.GET.get("page"))

    # Zbiór ID postów do ukrycia (spam) — template pokazuje placeholder zamiast treści
    spam_q = get_author_spam_filter(request.user)
    if spam_q:
        visible_post_ids = set(
            topic.posts.filter(spam_q).values_list("id", flat=True)
        )
    else:
        visible_post_ids = None  # None = pokaż wszystkie

    poll = getattr(topic, "poll", None)
    poll_now = timezone.now()
    poll_is_closed = False
    poll_user_votes = []
    poll_user_vote_option_ids = set()
    poll_can_vote = False
    poll_can_change_vote = False
    poll_show_results = False
    poll_max_option_votes = 0

    if poll is not None:
        poll_is_closed = poll.is_closed or (poll.ends_at is not None and poll.ends_at <= poll_now)
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
            or poll.total_votes == 0
            or poll_is_closed
            or bool(poll_user_votes)
        )
        poll_max_option_votes = max((option.vote_count for option in poll.options.all()), default=0)

    reply_form = ReplyForm() if not topic.is_locked else None

    is_mod = (
        request.user.is_authenticated
        and _is_moderator(request.user, topic.forum)
    )
    liked_post_ids = set()
    if request.user.is_authenticated:
        liked_post_ids = set(
            PostLike.objects.filter(
                user=request.user,
                post__topic=topic,
                post__in=page.object_list,
            ).values_list("post_id", flat=True)
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
        "poll_max_option_votes": poll_max_option_votes,
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


# ---------------------------------------------------------------------------
# Write views (login required)
# ---------------------------------------------------------------------------

@login_required
def new_topic(request, forum_id):
    """Create a new topic with its first post."""
    if request.user.is_root:
        return HttpResponseForbidden("Konto root nie może tworzyć postów.")
    forum = get_object_or_404(Forum, pk=forum_id)

    if request.method == "POST":
        form = NewTopicForm(request.POST)
        if form.is_valid():
            topic = Topic.objects.create(
                forum=forum,
                title=form.cleaned_data["title"],
                author=request.user,
            )
            post = _render_and_create_post(
                topic=topic,
                author=request.user,
                content_bbcode=form.cleaned_data["content"],
                post_order=1,
                author_ip=_get_client_ip(request),
            )
            _update_topic_stats(topic, post)
            _update_forum_stats(forum, post)
            _increment_user_post_count(request.user)
            poll_data = form.cleaned_data.get("poll_data")
            if poll_data:
                poll = Poll.objects.create(
                    topic=topic,
                    question=poll_data["question"],
                    ends_at=timezone.now() + timedelta(days=poll_data["duration_days"]),
                    allow_vote_change=poll_data["allow_vote_change"],
                    allow_multiple_choice=poll_data["allow_multiple_choice"],
                    is_closed=False,
                    is_archived_import=False,
                    total_votes=0,
                )
                PollOption.objects.bulk_create([
                    PollOption(
                        poll=poll,
                        option_text=option_text,
                        sort_order=index,
                    )
                    for index, option_text in enumerate(poll_data["options"], start=1)
                ])
            return redirect("topic_detail", topic_id=topic.pk)
    else:
        form = NewTopicForm()

    raw_poll_options = request.POST.getlist("poll_options") if request.method == "POST" else []
    poll_option_values = list(raw_poll_options) if raw_poll_options else ["", ""]
    while len(poll_option_values) < 2:
        poll_option_values.append("")
    poll_panel_open = bool(
        request.method == "POST" and (
            request.POST.get("poll_enabled")
            or (request.POST.get("poll_question") or "").strip()
            or any(v.strip() for v in raw_poll_options)
            or request.POST.get("poll_duration_days")
            or request.POST.get("poll_allow_vote_change")
            or request.POST.get("poll_allow_multiple_choice")
            or form.non_field_errors()
        )
    )

    return render(request, "board/new_topic.html", {
        "forum": forum,
        "form": form,
        "pinned_topic_posts": _get_global_pinned_topic_posts(),
        "poll_option_values": poll_option_values,
        "poll_panel_open": poll_panel_open,
        "poll_options_soft_limit": getattr(settings, "POLL_OPTIONS_SOFT_MAX", 32),
        "post_content_soft_limit": getattr(settings, "POST_CONTENT_SOFT_MAX_CHARS", 20_000),
    })


@login_required
def reply(request, topic_id):
    """Add a reply post to an existing topic."""
    if request.user.is_root:
        return HttpResponseForbidden("Konto root nie może tworzyć postów.")
    topic = get_object_or_404(Topic, pk=topic_id)

    if topic.is_locked:
        return redirect("topic_detail", topic_id=topic.pk)

    if request.method == "POST":
        form = ReplyForm(request.POST)
        if form.is_valid():
            next_order = topic.posts.count() + 1
            post = _render_and_create_post(
                topic=topic,
                author=request.user,
                content_bbcode=form.cleaned_data["content"],
                post_order=next_order,
                author_ip=_get_client_ip(request),
            )
            _update_topic_stats(topic, post)
            _update_forum_stats(topic.forum, post)
            _increment_user_post_count(request.user)

            # Redirect to the last page so user sees their post
            posts_per_page = getattr(settings, "POSTS_PER_PAGE", 20)
            last_page = (topic.posts.count() - 1) // posts_per_page + 1
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
        if not parsed_quote["phrases"] and not parsed_quote["terms"]:
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
            for term in parsed_quote["terms"]:
                search_rows = search_rows.filter(content_search_author_normalized__contains=term)
            matched_post_ids = [
                row.post_id for row in search_rows.only("post_id", "content_search_author_normalized")
                if _matches_search_text(
                    row.content_search_author_normalized,
                    parsed_quote["phrases"],
                    parsed_quote["terms"],
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
    """AJAX: validate and render BBCode text for the new-topic editor."""
    from django.http import JsonResponse
    from .bbcode import render as bbcode_render

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    get_object_or_404(Forum, pk=forum_id)
    text = request.POST.get("content", "")
    repaired, changes, errors = validate_post_content(text)
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
    })


@login_required
def quote_fragment(request, post_id):
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)

    post = get_object_or_404(Post.objects.only("pk", "content_bbcode"), pk=post_id)
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
    phrases = []
    terms = []
    skipped_terms = []

    for match in _SEARCH_PHRASE_RE.finditer(raw_query or ""):
        phrase = match.group(1)
        token = match.group(2)
        if phrase is not None:
            normalized_phrase = normalize_search_text(phrase)
            if normalized_phrase:
                phrases.append(normalized_phrase)
            continue

        normalized_token = normalize_search_text(token or "")
        if not normalized_token:
            continue
        if normalized_token in _SAFE_STOP_WORDS:
            skipped_terms.append(token)
            continue
        terms.append(normalized_token)

    return {
        "phrases": phrases,
        "terms": terms,
        "skipped_terms": skipped_terms,
    }


def _build_search_pattern(needle: str):
    return re.compile(
        _SEARCH_BOUNDARY.format(needle=re.escape(needle)),
        re.IGNORECASE,
    )


def _find_match_start(haystack: str, needle: str):
    match = _build_search_pattern(needle).search(haystack or "")
    return match.start() if match else -1


def _matches_search_text(text_norm: str, phrases: list[str], terms: list[str]) -> bool:
    for phrase in phrases:
        if _find_match_start(text_norm, phrase) == -1:
            return False
    for term in terms:
        if _find_match_start(text_norm, term) == -1:
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


def _build_search_snippet(text: str, phrases: list[str], terms: list[str], df_map: dict[str, int], width: int = 220):
    text = (text or "").strip()
    if not text:
        return ""

    text_norm = _normalize_for_match(text)
    anchor = None
    matched_needles = []

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


@login_required
def search(request):
    raw_query = (request.GET.get("q") or "").strip()
    forum_id_raw = (request.GET.get("forum_id") or "").strip()
    author_query_raw = (request.GET.get("author") or "").strip()
    search_mode = (request.GET.get("mode") or "posts").strip().lower()
    if search_mode not in {"posts", "topics"}:
        search_mode = "posts"
    search_filter = (request.GET.get("kind") or "all").strip().lower()
    allowed_filters = {
        "posts": {"all", "links", "youtube"},
        "topics": {"all", "polls"},
    }
    if search_filter not in allowed_filters[search_mode]:
        search_filter = "all"
    page_num = request.GET.get("page")

    indexed_forums = Forum.objects.filter(search_posts__isnull=False).distinct().order_by("title")
    selected_forum = None
    selected_author = None
    parsed = {"phrases": [], "terms": [], "skipped_terms": []}
    page = None
    info_message = ""
    snippet_width = max(80, getattr(settings, "SEARCH_SNIPPET_CHARS", 800))

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

    if (raw_query or search_filter != "all" or selected_author is not None) and not info_message:
        parsed = _parse_search_query(raw_query)
        if not parsed["phrases"] and not parsed["terms"]:
            if search_filter != "all" or selected_author is not None:
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
                qs = (
                    Topic.objects
                    .select_related("forum", "author", "last_post", "last_post__author", "poll")
                    .filter(forum__archive_level__lte=max_forum_level)
                )
                if selected_forum is not None:
                    qs = qs.filter(forum=selected_forum)
                if selected_author is not None:
                    qs = qs.filter(author=selected_author)
                if search_filter == "polls":
                    qs = qs.filter(poll__isnull=False)

                matched_topics = []
                for topic in qs.order_by("-created_at", "-pk"):
                    title_normalized = normalize_search_text(topic.title)
                    if parsed["phrases"] or parsed["terms"]:
                        if not _matches_search_text(
                            title_normalized,
                            parsed["phrases"],
                            parsed["terms"],
                        ):
                            continue
                    topic.title_html = _highlight_snippet(
                        topic.title,
                        parsed["phrases"] + parsed["terms"],
                    )
                    topic.has_poll = getattr(topic, "poll", None) is not None
                    matched_topics.append(topic)

                paginator = Paginator(matched_topics, getattr(settings, "TOPICS_PER_PAGE", 30))
                page = paginator.get_page(page_num)
            else:
                qs = (
                    PostSearchIndex.objects
                    .select_related("post", "author", "topic", "forum")
                    .filter(forum__archive_level__lte=max_forum_level)
                )
                if selected_forum is not None:
                    qs = qs.filter(forum=selected_forum)
                if selected_author is not None:
                    qs = qs.filter(author=selected_author)
                if search_filter == "links":
                    qs = qs.filter(has_link=True)
                elif search_filter == "youtube":
                    qs = qs.filter(has_youtube=True)

                for phrase in parsed["phrases"]:
                    qs = qs.filter(content_search_author_normalized__contains=phrase)
                for term in parsed["terms"]:
                    qs = qs.filter(content_search_author_normalized__contains=term)

                matched_rows = [
                    row for row in qs.order_by("-created_at", "-post_id")
                    if _matches_search_text(
                        row.content_search_author_normalized,
                        parsed["phrases"],
                        parsed["terms"],
                    )
                ]

                paginator = Paginator(matched_rows, getattr(settings, "POSTS_PER_PAGE", 20))
                page = paginator.get_page(page_num)
                if page is not None:
                    df_map = {}
                    for term in parsed["terms"]:
                        df_map[term] = sum(
                            1 for row in matched_rows
                            if _find_match_start(row.content_search_author_normalized, term) != -1
                        )
                    for row in page.object_list:
                        row.snippet_html = _build_search_snippet(
                            row.content_search_author,
                            parsed["phrases"],
                            parsed["terms"],
                            df_map,
                            width=snippet_width,
                        )

    return render(request, "board/search.html", {
        "indexed_forums": indexed_forums,
        "selected_forum": selected_forum,
        "selected_author": selected_author,
        "author_query": author_query_raw,
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
    topics = (
        Topic.objects.select_related("author", "forum", "last_post", "last_post__author")
        .filter(forum__archive_level__lte=max_forum_level)
        .order_by("-created_at", "-pk")
    )
    page = Paginator(topics, getattr(settings, "TOPICS_PER_PAGE", 30)).get_page(request.GET.get("page"))

    return render(request, "board/new_topics.html", {
        "page": page,
    })


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
    """User registration view."""
    from .models import SiteConfig
    if request.user.is_authenticated:
        return redirect("/")

    pending = request.session.get("register_pending")
    start_form = RegisterStartForm(initial=pending or None)
    finish_form = RegisterFinishForm()
    sent = False
    test_code = None
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

    def send_registration_code(username: str, email: str):
        nonlocal sent, test_code
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = timezone.now()
        request.session["register_code"] = code
        request.session["register_code_sent_at"] = now.isoformat()
        request.session["register_code_expires_at"] = (
            now + timedelta(minutes=30)
        ).isoformat()
        request.session["register_code_attempts"] = 0
        request.session.modified = True

        cfg = SiteConfig.get()
        if getattr(settings, "TEST_MODE", False) or cfg.reset_mode == SiteConfig.RESET_POPUP:
            sent = True
            test_code = code
            return

        send_mail(
            subject="Kod rejestracyjny",
            message=(
                f"Twój kod rejestracyjny dla konta {username}: {code}\n\n"
                f"Kod jest ważny przez 30 minut."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum"),
            recipient_list=[email],
            fail_silently=False,
        )
        sent = True

    if request.method == "GET" and request.GET.get("reset") == "1":
        clear_pending_registration()
        pending = None

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "start":
            start_form = RegisterStartForm(request.POST)
            if start_form.is_valid():
                clear_pending_registration()
                request.session["register_pending"] = {
                    "username": start_form.cleaned_data["username"],
                    "email": start_form.cleaned_data["email"],
                }
                request.session.modified = True
                return redirect("register")
        elif action == "send_code":
            if not pending:
                return redirect("register")
            start_form = RegisterStartForm(initial=pending)
            finish_form = RegisterFinishForm()
            send_registration_code(pending["username"], pending["email"])
        elif action == "finish":
            if not pending:
                return redirect("register")
            start_form = RegisterStartForm(initial=pending)
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
                username = pending["username"]
                email = pending["email"]
                # Re-check uniqueness at final submit to avoid races.
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
                elif not code or not expires_at or timezone.now() >= expires_at:
                    error = "Kod wygasł. Wyślij nowy kod."
                elif attempts >= 10:
                    error = "Zbyt wiele błędnych prób kodu. Wyślij nowy kod."
                elif finish_form.cleaned_data["code"] != code:
                    request.session["register_code_attempts"] = attempts + 1
                    request.session.modified = True
                    error = "Nieprawidłowy kod."
                else:
                    user = User(
                        username=username,
                        email=email,
                        is_active=True,
                    )
                    password = finish_form.cleaned_data["password1"]
                    if finish_form.cleaned_data.get("password_is_prehashed") == "1":
                        user.set_password(password)
                    else:
                        user.set_password(prehash_password(password, username))
                    user.save()
                    clear_pending_registration()
                    login(request, user)
                    return redirect("/")

    if pending:
        start_form = RegisterStartForm(initial=pending)

    return render(request, "registration/register.html", {
        "start_form": start_form,
        "finish_form": finish_form,
        "pending": pending,
        "sent": sent,
        "test_code": test_code,
        "error": error,
    })


def activate_ghost(request):
    """Step 2 of ghost activation: user proves email ownership."""
    from .models import User
    username = request.POST.get("username") or request.GET.get("username", "")
    try:
        user = User.objects.get(username=username, is_ghost=False, is_active=False)
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
            user.is_ghost = False
            user.is_active = True
            user.save(update_fields=["is_ghost", "is_active"])
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
    if request.method == "POST":
        email_input = request.POST.get("email", "").strip().lower()
        if email_input:
            user = User.objects.filter(is_ghost=True, email=email_input).first()

            if user:
                token_obj, _ = ActivationToken.objects.get_or_create(
                    user=user,
                    defaults={
                        "token": secrets.token_urlsafe(48),
                        "expires_at": timezone.now() + timedelta(hours=24),
                    },
                )
                # Refresh token
                token_obj.token = secrets.token_urlsafe(48)
                token_obj.expires_at = timezone.now() + timedelta(hours=24)
                token_obj.failed_attempts = 0
                token_obj.window_start = None
                token_obj.save()

                activation_url = request.build_absolute_uri(f"/activate/{token_obj.token}/")

                if getattr(settings, "TEST_MODE", False):
                    # TEST_MODE: aktywuj od razu, wyświetl nick (tylko na dev)
                    user.is_ghost = False
                    user.is_active = True
                    user.save(update_fields=["is_ghost", "is_active"])
                    login(request, user)
                    return render(request, "registration/find_account.html", {
                        "test_mode_username": user.username,
                        "success": True,
                    })

                send_mail(
                    subject="Twoje konto na forum",
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
        # Zawsze ta sama odpowiedź — nie zdradza czy email jest w bazie
        sent = True

    return render(request, "registration/find_account.html", {"sent": sent})


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
    user.is_ghost = False
    user.is_active = True
    user.save(update_fields=["is_ghost", "is_active"])
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
    return user.is_root or forum.moderators.filter(pk=user.pk).exists()


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


def _send_reset_code_email(user, code: str, recipient_email: str) -> None:
    from django.utils.formats import date_format
    sent_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")
    send_mail(
        subject="[Forum] Kod do resetowania hasła",
        message=(
            f"Nick: {user.username}\n"
            f"Kod: {code}\n"
            f"Wysłano: {sent_at}\n\n"
            f"Kod jest ważny przez {PasswordResetCode.CODE_EXPIRY_HOURS} godziny.\n"
            f"Wejdź na forum → Zresetuj hasło i wpisz ten kod razem z nowym hasłem.\n\n"
            f"Jeśli to nie Ty prosiłeś — zignoruj tę wiadomość."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@forum"),
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

    - Wrong password: increment fail counter, show error (max 20/h).
    - Unusable password (null, admin-invalidated): redirect to reset flow.
    - 'Forgot password' link on page → user navigates to reset voluntarily.
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
                login(request, user)
                return redirect(request.POST.get("next") or request.GET.get("next") or "/")

            # Auth failed — check if password is unusable (admin-invalidated)
            try:
                candidate = ForumUser.objects.get(username=username)
                if not candidate.has_usable_password():
                    return redirect(
                        f"/reset-hasla/?username={candidate.username}&reason=invalidated"
                    )
            except ForumUser.DoesNotExist:
                pass

            _record_login_fail(username)
            error = "Nieprawidłowy nick lub hasło."

    return render(request, "registration/login.html", {
        "error": error,
        "next": request.GET.get("next", ""),
    })


def request_reset(request):
    """Step 1: user asks for a reset code. Sends 6-digit code by email or shows popup."""
    from django.http import JsonResponse
    from .models import User as ForumUser, SiteConfig

    reason = request.GET.get("reason", "")
    prefill_username = request.GET.get("username", "")

    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        username = request.POST.get("username", "").strip()

        def ajax_err(msg):
            return JsonResponse({"ok": False, "error": msg})

        try:
            user = ForumUser.objects.get(username=username)
        except ForumUser.DoesNotExist:
            if is_ajax:
                return JsonResponse({"ok": False, "error": "Nie znaleziono konta o tym nicku."})
            return render(request, "registration/request_reset.html", {
                "error": "Nie znaleziono konta o tym nicku.",
                "reason": reason, "prefill_username": username,
            })

        if not user.email:
            msg = "To konto nie ma adresu email. Skontaktuj się z administratorem."
            if is_ajax:
                return ajax_err(msg)
            return render(request, "registration/request_reset.html", {
                "error": msg, "reason": reason, "prefill_username": username,
            })

        allowed, _ = _can_send_reset_code(user)
        if not allowed:
            msg = (f"Wysłano już {PasswordResetCode.MAX_PER_HOUR} kody w ciągu ostatniej godziny. "
                   "Sprawdź skrzynkę lub spróbuj ponownie za chwilę.")
            if is_ajax:
                return ajax_err(msg)
            return render(request, "registration/request_reset.html", {
                "error": msg, "reason": reason, "prefill_username": username,
            })

        code = _generate_reset_code()
        expires = timezone.now() + timedelta(hours=PasswordResetCode.CODE_EXPIRY_HOURS)
        PasswordResetCode.objects.create(user=user, code=code, expires_at=expires)

        cfg = SiteConfig.get()
        use_popup = (cfg.reset_mode == SiteConfig.RESET_POPUP)

        if use_popup:
            sent_at = timezone.now().strftime("%Y-%m-%d %H:%M")
            if is_ajax:
                return JsonResponse({
                    "ok": True,
                    "popup": True,
                    "code": code,
                    "username": username,
                    "sent_at": sent_at,
                    "do_reset_url": f"/ustaw-haslo/?username={username}",
                })
            # Non-AJAX fallback
            return render(request, "registration/request_reset.html", {
                "popup_code": code,
                "popup_username": username,
                "popup_sent_at": sent_at,
                "do_reset_url": f"/ustaw-haslo/?username={username}",
                "reason": reason,
            })

        _send_reset_code_email(user, code, user.email)
        if is_ajax:
            return JsonResponse({"ok": True, "popup": False,
                                 "email_mask": mask_email(user.email)})
        return render(request, "registration/request_reset.html", {
            "sent": True, "email_mask": mask_email(user.email),
            "reason": reason,
        })

    return render(request, "registration/request_reset.html", {
        "reason": reason,
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
                code_obj = _find_valid_code(user, code_input)
                if code_obj is None:
                    error = "Nieprawidłowy lub wygasły kod."
                else:
                    from .auth_utils import prehash_password
                    is_prehashed = request.POST.get("password_is_prehashed") == "1"
                    if not is_prehashed:
                        password1 = prehash_password(password1, username)
                    user.set_password(password1)
                    # Activate ghost accounts on password reset (user proved email access)
                    update_fields = ["password"]
                    if not user.is_active:
                        user.is_active = True
                        user.is_ghost = False
                        update_fields += ["is_active", "is_ghost"]
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
                # Anti-spam: check sender's outbox limit
                outbox_limit = getattr(settings, "PM_OUTBOX_LIMIT", 50)
                in_flight = PrivateMessage.objects.filter(
                    sender=request.user, delivered_at=None
                ).count()
                if in_flight >= outbox_limit:
                    error = (
                        f"Masz już {in_flight} wiadomości oczekujących na dostarczenie "
                        f"(limit: {outbox_limit}). Poczekaj aż odbiorcy je odbiorą."
                    )
                else:
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

def root_config(request):
    """Root-only view to toggle site-wide settings."""
    from .models import SiteConfig
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
            cfg.reset_mode = request.POST.get("reset_mode", SiteConfig.RESET_EMAIL)
            cfg.show_switch_link = (request.POST.get("show_switch_link") == "1")
            try:
                cfg.search_snippet_chars = max(
                    80,
                    int(request.POST.get("search_snippet_chars", cfg.search_snippet_chars)),
                )
            except (TypeError, ValueError):
                messages.error(request, "Długość snippetu musi być liczbą całkowitą.")
                return redirect("root_config")
            cfg.save()
        return redirect("root_config")

    return render(request, "board/root_config.html", {
        "cfg": cfg,
        "SiteConfig": SiteConfig,
        "reset_codes_count": PasswordResetCode.objects.count(),
        "all_users": all_users,
        "empty_users": empty_users,
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
