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


class PostReport(models.Model):
    """User report of a post (spam, abuse, etc.).

    Stub for future implementation. The web version will have a moderation
    queue that reads these. For now the endpoint POST /api/v1/posts/{id}/report
    creates records here, and GET /api/v1/mod/reports returns them.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    post = models.ForeignKey(
        "board.Post",
        on_delete=models.CASCADE,
        related_name="reports",
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reports_made",
    )
    reason = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="reports_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "api_post_reports"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["post"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return f"Report #{self.pk} on Post #{self.post_id} [{self.status}]"
