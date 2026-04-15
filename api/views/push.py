"""FCM push notification token management.

TODO: Actual FCM push sending is not implemented yet.
Notifications are currently delivered by polling:
  GET /api/v1/notifications — client polls when app is opened.

When the notification system is built (for both web and Android),
the tokens stored here will be used to send push notifications via
Google FCM API. See docs/api.md for the full TODO description.
"""

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from api import response as R


class PushRegisterView(APIView):
    """Save FCM device token for push notifications (stored, not yet used)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = (request.data.get("token") or "").strip()
        if not token:
            return R.error("MISSING_FIELD", "Wymagane pole: token.")

        from api.models import FcmToken
        FcmToken.objects.update_or_create(
            token=token,
            defaults={"user": request.user},
        )
        return R.ok({"registered": True})


class PushUnregisterView(APIView):
    """Remove FCM device token (e.g. on logout)."""
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        token = (request.data.get("token") or "").strip()
        if not token:
            return R.error("MISSING_FIELD", "Wymagane pole: token.")

        from api.models import FcmToken
        FcmToken.objects.filter(user=request.user, token=token).delete()
        return R.ok({"unregistered": True})
