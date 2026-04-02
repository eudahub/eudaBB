import secrets
from datetime import timedelta

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.utils import timezone
from django.conf import settings

from .models import Section, Forum, Topic, Post, ActivationToken
from .forms import RegisterForm, NewTopicForm, ReplyForm
from .email_utils import verify_email, mask_email_variants, fix_email_mask_if_needed
from .spam_utils import get_author_spam_filter, filter_forums
from . import bbcode as bbcode_renderer


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


def _render_and_create_post(topic: Topic, author, content_bbcode: str, post_order: int) -> Post:
    """Create a Post with rendered HTML cache."""
    content_html = bbcode_renderer.render(content_bbcode)
    return Post.objects.create(
        topic=topic,
        author=author,
        content_bbcode=content_bbcode,
        content_html=content_html,
        post_order=post_order,
    )


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
            section._visible_forums = visible
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
    topic = get_object_or_404(Topic, pk=topic_id)

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

    reply_form = ReplyForm() if not topic.is_locked else None

    return render(request, "board/topic_detail.html", {
        "topic": topic,
        "forum": topic.forum,
        "page": page,
        "reply_form": reply_form,
        "visible_post_ids": visible_post_ids,
    })


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
            )
            _update_topic_stats(topic, post)
            _update_forum_stats(forum, post)
            _increment_user_post_count(request.user)
            return redirect("topic_detail", topic_id=topic.pk)
    else:
        form = NewTopicForm()

    return render(request, "board/new_topic.html", {"forum": forum, "form": form})


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

    return render(request, "board/reply.html", {"topic": topic, "form": form})


# ---------------------------------------------------------------------------
# TODO: Search
# ---------------------------------------------------------------------------
# Search must require login (@login_required) — DDoS protection.
# Full-text search is expensive; anonymous access would allow trivial amplification.
#
# Implementation notes:
# - Use PostgreSQL full-text search (SearchVector on Post.content_bbcode)
# - Filter results by forum access_level <= request.user.archive_access
# - Paginate results (reuse POSTS_PER_PAGE)
# - Client-side Argon2 + server-side SHA3-256 for login (see auth TODO below)
# ---------------------------------------------------------------------------


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
    if request.user.is_authenticated:
        return redirect("/")

    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            ghost_username = getattr(form, "_ghost_username", None)
            if ghost_username:
                # Ghost account: save password but don't activate yet.
                # User must verify email ownership via activate_ghost view.
                user = User.objects.get(username=ghost_username)
                user.set_password(form.cleaned_data["password1"])
                user.is_active = False
                user.save(update_fields=["password", "is_active"])
                return render(request, "registration/register.html", {
                    "form": form,
                    "ghost_username": ghost_username,
                    "email_mask": user.email_mask,
                })
            raw_email = form.cleaned_data.get("email", "")
            variants = mask_email_variants(raw_email)
            mask_variant = request.POST.get("mask_variant", "")
            if variants and mask_variant not in variants:
                # Short email — ask user to pick a mask variant
                return render(request, "registration/register.html", {
                    "form": form,
                    "mask_variants": variants,
                })
            user = form.save(mask_variant=mask_variant or None)
            login(request, user)
            return redirect("/")
    else:
        form = RegisterForm()

    return render(request, "registration/register.html", {"form": form})


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
                "email_mask": user.email_mask,
                "error": f"Zbyt wiele prób. Spróbuj ponownie za {remaining} minut.",
            })

        email_input = request.POST.get("email", "").strip()
        if not user.email_hash or not verify_email(email_input, user.email_hash):
            token_obj.record_failed_attempt()
            remaining_attempts = ActivationToken.MAX_ATTEMPTS - token_obj.failed_attempts
            return render(request, "registration/activate_ghost.html", {
                "username": username,
                "email_mask": user.email_mask,
                "error": f"Podany email nie pasuje do konta. Pozostało prób: {max(remaining_attempts, 0)}.",
            })

        # Email matches — napraw maskę jeśli niezgodna (błąd w bazie)
        fix_email_mask_if_needed(user, email_input)

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
        "email_mask": user.email_mask,
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
            # Deterministyczny hash → bezpośredni lookup O(1)
            from .email_utils import hash_email
            h = hash_email(email_input)
            user = User.objects.filter(is_ghost=True, email_hash=h).first()

            if user:
                fix_email_mask_if_needed(user, email_input)
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
