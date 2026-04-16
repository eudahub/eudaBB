"""In-app notification endpoints (polling — no push).

Android calls GET /api/v1/notifications on app open to show the badge count
and notification list. No FCM / external push involved.
"""

from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from board.models import Notification, Post
from api import response as R
from api.serializers import NotificationSerializer


class NotificationListView(APIView):
    """GET /api/v1/notifications?page=1

    Returns unread notifications for the authenticated user.
    PENDING_QUEUE type is included only when the pending queue is non-empty.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            Notification.objects
            .filter(recipient=request.user, is_read=False)
            .select_related("actor", "post__topic", "pm")
            .order_by("-created_at")
        )
        # Suppress PENDING_QUEUE entries when queue is actually empty
        has_pending = Post.objects.filter(is_pending=True).exists()
        if not has_pending:
            qs = qs.exclude(notif_type=Notification.Type.PENDING_QUEUE)

        return R.paginate(qs, request, NotificationSerializer, per_page=30)


class NotificationMarkReadView(APIView):
    """PUT /api/v1/notifications/{id}/read

    Marks a single notification as read.
    """
    permission_classes = [IsAuthenticated]

    def put(self, request, notification_id):
        notif = get_object_or_404(
            Notification, pk=notification_id, recipient=request.user
        )
        if not notif.is_read:
            notif.is_read = True
            notif.save(update_fields=["is_read"])
        return R.ok({"id": notif.pk, "is_read": True})


class NotificationMarkAllReadView(APIView):
    """PUT /api/v1/notifications/read-all

    Marks all unread notifications as read.
    Keeps PENDING_QUEUE notifications if the pending queue is still non-empty.
    """
    permission_classes = [IsAuthenticated]

    def put(self, request):
        qs = Notification.objects.filter(recipient=request.user, is_read=False)
        has_pending = Post.objects.filter(is_pending=True).exists()
        if has_pending:
            qs = qs.exclude(notif_type=Notification.Type.PENDING_QUEUE)
        marked = qs.update(is_read=True)
        return R.ok({"marked": marked})
