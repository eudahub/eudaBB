from django.db import models
from django.conf import settings


class FcmToken(models.Model):
    """FCM (Firebase Cloud Messaging) device token for push notifications.

    TODO: Push notifications are not yet implemented.
    The endpoint POST /api/v1/push/register stores the token here.
    When the notification system is built (for both web and Android),
    tokens stored here will be used to send push notifications via FCM API.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fcm_tokens",
    )
    token = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "api_fcm_tokens"

    def __str__(self):
        return f"FCM token for {self.user_id}"


# PostReport removed — use board.PostReport (table: forum_post_report).
# Unified model shared by web and API views.
