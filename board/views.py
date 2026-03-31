from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.utils import timezone
from django.conf import settings

from .models import Section, Forum, Topic, Post
from .forms import RegisterForm, NewTopicForm, ReplyForm
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
    sections = Section.objects.prefetch_related(
        "forums",
        "forums__last_post",
        "forums__last_post__author",
    ).all()
    return render(request, "board/index.html", {"sections": sections})


def forum_detail(request, forum_id):
    """Topic list for a single forum, paginated."""
    forum = get_object_or_404(Forum, pk=forum_id, is_visible=True)
    topics_qs = forum.topics.select_related("author", "last_post", "last_post__author")
    paginator = Paginator(topics_qs, getattr(settings, "TOPICS_PER_PAGE", 30))
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "board/forum_detail.html", {"forum": forum, "page": page})


def topic_detail(request, topic_id):
    """Post list for a single topic, paginated. Increments view counter."""
    topic = get_object_or_404(Topic, pk=topic_id)

    # Increment view counter (simple version — no dedup)
    Topic.objects.filter(pk=topic_id).update(view_count=topic.view_count + 1)

    posts_qs = topic.posts.select_related("author", "updated_by")
    paginator = Paginator(posts_qs, getattr(settings, "POSTS_PER_PAGE", 20))
    page = paginator.get_page(request.GET.get("page"))

    reply_form = ReplyForm() if not topic.is_locked else None

    return render(request, "board/topic_detail.html", {
        "topic": topic,
        "forum": topic.forum,
        "page": page,
        "reply_form": reply_form,
    })


# ---------------------------------------------------------------------------
# Write views (login required)
# ---------------------------------------------------------------------------

@login_required
def new_topic(request, forum_id):
    """Create a new topic with its first post."""
    forum = get_object_or_404(Forum, pk=forum_id, is_visible=True)

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
# Auth
# ---------------------------------------------------------------------------

def register(request):
    """User registration view."""
    if request.user.is_authenticated:
        return redirect("/")

    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("/")
    else:
        form = RegisterForm()

    return render(request, "registration/register.html", {"form": form})
