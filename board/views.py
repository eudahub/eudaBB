import secrets
from datetime import timedelta

from django.db import models as django_models
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.utils import timezone
from django.conf import settings

from .models import Section, Forum, Topic, Post, User, ActivationToken, BlockedIP, PasswordResetCode, PrivateMessage, PrivateMessageBox
from .forms import (
    RegisterForm, RegisterStartForm, RegisterFinishForm,
    NewTopicForm, ReplyForm, validate_post_content,
)
from .email_utils import mask_email, mask_email_variants
from .spam_utils import get_author_spam_filter, filter_forums
from .middleware import invalidate_blocked_ips_cache
from .auth_utils import prehash_password
from .username_utils import normalize


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


def _get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def _render_and_create_post(topic: Topic, author, content_bbcode: str,
                             post_order: int, author_ip: str = None) -> Post:
    retain_until = _retain_until(flagged=False) if author_ip else None
    return Post.objects.create(
        topic=topic,
        author=author,
        content_bbcode=content_bbcode,
        post_order=post_order,
        author_ip=author_ip,
        ip_retain_until=retain_until,
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

    is_mod = (
        request.user.is_authenticated
        and _is_moderator(request.user, topic.forum)
    )

    return render(request, "board/topic_detail.html", {
        "topic": topic,
        "forum": topic.forum,
        "page": page,
        "reply_form": reply_form,
        "visible_post_ids": visible_post_ids,
        "is_moderator": is_mod,
        "dangerous_days": getattr(settings, "IP_BAN_DANGEROUS_DAYS", 90),
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
                author_ip=_get_client_ip(request),
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
    recent_posts = (
        topic.posts.select_related("author")
        .order_by("-post_order")[:posts_per_page]
    )
    return render(request, "board/reply.html", {
        "topic": topic,
        "form": form,
        "recent_posts": recent_posts,
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
                    # Repair + validate BBCode
                    from .bbcode_lint import repair_and_validate
                    repaired_content, changes, lint_errors = repair_and_validate(raw_content)
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
            from .bbcode_lint import repair_and_validate
            repaired_content, changes, lint_errors = repair_and_validate(raw_content)
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

    current_content = decompress(pm.content_compressed)
    return render(request, "board/pm_edit.html", {
        "box": box,
        "current_content": current_content,
        "error": error,
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


# ---------------------------------------------------------------------------
# Quote link: redirect to the source post
# ---------------------------------------------------------------------------

def root_config(request):
    """Root-only view to toggle site-wide settings."""
    from .models import SiteConfig
    if not request.user.is_authenticated or not request.user.is_root:
        return HttpResponseForbidden()

    cfg = SiteConfig.get()
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
            cfg.save()
        return redirect("root_config")

    return render(request, "board/root_config.html", {
        "cfg": cfg,
        "SiteConfig": SiteConfig,
        "reset_codes_count": PasswordResetCode.objects.count(),
        "empty_users": empty_users,
    })


def goto_post(request, post_id):
    """Redirect to the topic page anchored at the given post."""
    from django.http import Http404
    try:
        post = Post.objects.select_related("topic").get(pk=post_id)
    except Post.DoesNotExist:
        raise Http404
    url = redirect("topic_detail", topic_id=post.topic_id).url + f"#post-{post_id}"
    from django.http import HttpResponseRedirect
    return HttpResponseRedirect(url)
