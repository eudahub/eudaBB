"""Notification endpoints.

TODO: A proper notification model (replies, quotes, mentions) is not yet implemented.
Notifications will be built together with the web version.
For now the Android app polls this endpoint on open — always returns empty list.
"""

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api import response as R


class NotificationListView(APIView):
    """List notifications (polling — no push yet).

    TODO: Implement after the notification model is built.
    Returns empty list for now.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return R.ok(
            [],
            pagination={"page": 1, "per_page": 20, "total_pages": 0, "total_items": 0},
        )


class NotificationMarkReadView(APIView):
    """Mark single notification as read. TODO: stub."""
    permission_classes = [IsAuthenticated]

    def put(self, request, notification_id):
        return R.error("NOT_IMPLEMENTED", "Powiadomienia nie są jeszcze zaimplementowane.", 501)


class NotificationMarkAllReadView(APIView):
    """Mark all notifications as read. TODO: stub."""
    permission_classes = [IsAuthenticated]

    def put(self, request):
        return R.ok({"marked": 0})
