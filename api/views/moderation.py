"""Moderation endpoints — require moderator or admin role."""

from datetime import timedelta
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.views import APIView

from board.models import Topic, Post, User
from api import response as R
from api.permissions import IsModerator, IsAdmin
from api.serializers import PostSerializer, PostReportSerializer


# ---------------------------------------------------------------------------
# DELETE /api/v1/mod/posts/{post_id}
# ---------------------------------------------------------------------------

class ModDeletePostView(APIView):
    """Moderator soft-delete (hard delete) of any post."""
    permission_classes = [IsModerator]

    def delete(self, request, post_id):
        post = get_object_or_404(Post, pk=post_id)
        topic = post.topic

        post.delete()

        remaining = Post.objects.filter(topic=topic).count()
        if remaining == 0:
            topic.delete()
        else:
            topic.reply_count = max(0, remaining - 1)
            last = Post.objects.filter(topic=topic).order_by("-created_at").first()
            topic.last_post = last
            topic.last_post_at = last.created_at if last else None
            topic.save(update_fields=["reply_count", "last_post", "last_post_at"])

        return R.ok({"deleted": True})


# ---------------------------------------------------------------------------
# PUT /api/v1/mod/posts/{post_id}  — admin edit
# ---------------------------------------------------------------------------

class ModEditPostView(APIView):
    """Admin-only: edit any post."""
    permission_classes = [IsAdmin]

    def put(self, request, post_id):
        post = get_object_or_404(Post, pk=post_id)
        content = (request.data.get("content") or "").strip()
        if not content:
            return R.error("MISSING_FIELD", "Wymagane pole: content.")

        from board.forms import validate_post_content
        repaired, _changes, errors = validate_post_content(content, original_size=len(post.content_bbcode))
        if errors:
            return R.error("VALIDATION_ERROR", "; ".join(errors))

        post.content_bbcode = repaired
        post.updated_at = timezone.now()
        post.updated_by = request.user
        post.edit_count += 1
        post.save(update_fields=["content_bbcode", "updated_at", "updated_by", "edit_count"])

        return R.ok(PostSerializer(post, context={"request": request}).data)


# ---------------------------------------------------------------------------
# PUT /api/v1/mod/threads/{topic_id}/lock|unlock
# ---------------------------------------------------------------------------

class ModLockView(APIView):
    permission_classes = [IsModerator]

    def put(self, request, topic_id):
        topic = get_object_or_404(Topic, pk=topic_id)
        topic.is_locked = True
        topic.save(update_fields=["is_locked"])
        return R.ok({"locked": True})


class ModUnlockView(APIView):
    permission_classes = [IsModerator]

    def put(self, request, topic_id):
        topic = get_object_or_404(Topic, pk=topic_id)
        topic.is_locked = False
        topic.save(update_fields=["is_locked"])
        return R.ok({"locked": False})


# ---------------------------------------------------------------------------
# PUT /api/v1/mod/threads/{topic_id}/pin|unpin
# ---------------------------------------------------------------------------

class ModPinView(APIView):
    permission_classes = [IsModerator]

    def put(self, request, topic_id):
        topic = get_object_or_404(Topic, pk=topic_id)
        topic.topic_type = Topic.TopicType.STICKY
        topic.save(update_fields=["topic_type"])
        return R.ok({"pinned": True})


class ModUnpinView(APIView):
    permission_classes = [IsModerator]

    def put(self, request, topic_id):
        topic = get_object_or_404(Topic, pk=topic_id)
        if topic.topic_type == Topic.TopicType.STICKY:
            topic.topic_type = Topic.TopicType.NORMAL
            topic.save(update_fields=["topic_type"])
        return R.ok({"pinned": False})


# ---------------------------------------------------------------------------
# PUT /api/v1/mod/threads/{topic_id}/move  — admin only
# ---------------------------------------------------------------------------

class ModMoveThreadView(APIView):
    permission_classes = [IsAdmin]

    def put(self, request, topic_id):
        topic = get_object_or_404(Topic, pk=topic_id)
        target_forum_id = request.data.get("forum_id")
        if not target_forum_id:
            return R.error("MISSING_FIELD", "Wymagane pole: forum_id.")

        from board.models import Forum
        target = get_object_or_404(Forum, pk=target_forum_id)
        old_forum = topic.forum

        topic.forum = target
        topic.save(update_fields=["forum"])

        # Refresh counters on both forums
        for forum in [old_forum, target]:
            forum.topic_count = Topic.objects.filter(forum=forum).count()
            forum.post_count = Post.objects.filter(topic__forum=forum).count()
            forum.save(update_fields=["topic_count", "post_count"])

        return R.ok({"moved_to_forum_id": target.pk})


# ---------------------------------------------------------------------------
# POST /api/v1/mod/users/{user_id}/ban
# ---------------------------------------------------------------------------

class ModBanView(APIView):
    """Ban a user temporarily (duration_hours > 0) or permanently (duration_hours = 0)."""
    permission_classes = [IsModerator]

    def post(self, request, user_id):
        target = get_object_or_404(User, pk=user_id)

        if target.is_root:
            return R.error("FORBIDDEN", "Nie można zablokować konta root.", 403)

        if target.role >= User.ROLE_ADMIN and not request.user.is_root:
            return R.error("FORBIDDEN", "Tylko root może blokować administratorów.", 403)

        try:
            duration_hours = int(request.data.get("duration_hours", 24))
        except (TypeError, ValueError):
            return R.error("INVALID_FIELD", "duration_hours musi być liczbą całkowitą.")

        reason = (request.data.get("reason") or "").strip()

        target.is_active = False
        target.ban_reason = reason
        if duration_hours == 0:
            target.banned_until = None  # permanent
        else:
            target.banned_until = timezone.now() + timedelta(hours=duration_hours)
        target.save(update_fields=["is_active", "ban_reason", "banned_until"])

        return R.ok({
            "banned": True,
            "banned_until": target.banned_until.isoformat() if target.banned_until else None,
            "permanent": duration_hours == 0,
        })


# ---------------------------------------------------------------------------
# DELETE /api/v1/mod/users/{user_id}/ban
# ---------------------------------------------------------------------------

class ModUnbanView(APIView):
    """Lift a ban."""
    permission_classes = [IsModerator]

    def delete(self, request, user_id):
        target = get_object_or_404(User, pk=user_id)
        target.is_active = True
        target.banned_until = None
        target.ban_reason = ""
        target.save(update_fields=["is_active", "banned_until", "ban_reason"])
        return R.ok({"banned": False})


# ---------------------------------------------------------------------------
# GET /api/v1/mod/reports
# ---------------------------------------------------------------------------

class ModReportListView(APIView):
    """List open reports for moderation queue."""
    permission_classes = [IsModerator]

    def get(self, request):
        from api.models import PostReport
        status_filter = request.query_params.get("status", "open")
        qs = (
            PostReport.objects
            .filter(status=status_filter)
            .select_related("post", "reporter")
            .order_by("-created_at")
        )
        return R.paginate(qs, request, PostReportSerializer, per_page=30)


# ---------------------------------------------------------------------------
# PUT /api/v1/mod/reports/{report_id}/resolve|dismiss
# ---------------------------------------------------------------------------

class ModReportResolveView(APIView):
    permission_classes = [IsModerator]

    def put(self, request, report_id):
        from api.models import PostReport
        report = get_object_or_404(PostReport, pk=report_id)
        report.status = PostReport.Status.RESOLVED
        report.resolved_by = request.user
        report.resolved_at = timezone.now()
        report.save(update_fields=["status", "resolved_by", "resolved_at"])
        return R.ok({"status": "resolved"})


class ModReportDismissView(APIView):
    permission_classes = [IsModerator]

    def put(self, request, report_id):
        from api.models import PostReport
        report = get_object_or_404(PostReport, pk=report_id)
        report.status = PostReport.Status.DISMISSED
        report.resolved_by = request.user
        report.resolved_at = timezone.now()
        report.save(update_fields=["status", "resolved_by", "resolved_at"])
        return R.ok({"status": "dismissed"})
