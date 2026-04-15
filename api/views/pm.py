"""Private message endpoints.

eudaBB's PM model is flat (each message is independent, no threaded conversations).
The API exposes the inbox as a list of "conversations" where each item is one PM box entry.
POST /api/v1/conversations/{id}/messages replies to the original sender with a new PM.
"""

import zlib
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from board.models import User, PrivateMessage, PrivateMessageBox
from api import response as R
from api.serializers import PMBoxSerializer, PMDetailSerializer


# ---------------------------------------------------------------------------
# GET /api/v1/conversations
# ---------------------------------------------------------------------------

class ConversationListView(APIView):
    """Inbox: list of received PMs (most recent first)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            PrivateMessageBox.objects
            .filter(owner=request.user, box_type=PrivateMessageBox.BoxType.INBOX)
            .select_related("message__sender", "message__recipient")
            .order_by("-message__created_at")
        )
        return R.paginate(qs, request, PMBoxSerializer, per_page=30, context={"request": request})


# ---------------------------------------------------------------------------
# GET /api/v1/conversations/{box_id}
# ---------------------------------------------------------------------------

class ConversationDetailView(APIView):
    """Full PM message."""
    permission_classes = [IsAuthenticated]

    def get(self, request, box_id):
        box = get_object_or_404(
            PrivateMessageBox.objects.select_related("message__sender", "message__recipient"),
            pk=box_id,
            owner=request.user,
        )
        # Mark as read
        if not box.is_read:
            box.is_read = True
            box.save(update_fields=["is_read"])
            if box.message.delivered_at is None:
                box.message.delivered_at = timezone.now()
                box.message.save(update_fields=["delivered_at"])

        return R.ok(PMDetailSerializer(box, context={"request": request}).data)


# ---------------------------------------------------------------------------
# POST /api/v1/conversations  — send new PM
# ---------------------------------------------------------------------------

class SendPMView(APIView):
    """Send a new private message."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        recipient_username = (request.data.get("recipient") or "").strip()
        subject = (request.data.get("subject") or "").strip()
        content = (request.data.get("content") or "").strip()

        if not recipient_username:
            return R.error("MISSING_FIELD", "Wymagane pole: recipient.")
        if not subject:
            return R.error("MISSING_FIELD", "Wymagane pole: subject.")
        if not content:
            return R.error("MISSING_FIELD", "Wymagane pole: content.")
        if len(subject) > 255:
            return R.error("FIELD_TOO_LONG", "Temat może mieć max 255 znaków.")

        try:
            recipient = User.objects.get(username=recipient_username)
        except User.DoesNotExist:
            return R.error("USER_NOT_FOUND", "Nie znaleziono użytkownika.", 404)

        if recipient.pk == request.user.pk:
            return R.error("INVALID_RECIPIENT", "Nie możesz wysłać wiadomości do siebie.")

        from board.forms import validate_pm_content
        repaired, _changes, errors = validate_pm_content(content)
        if errors:
            return R.error("VALIDATION_ERROR", "; ".join(errors))

        content_compressed = zlib.compress(repaired.encode("utf-8"))

        msg = PrivateMessage.objects.create(
            sender=request.user,
            recipient=recipient,
            subject=subject,
            content_compressed=content_compressed,
        )
        # Sender's outbox entry
        PrivateMessageBox.objects.create(
            message=msg,
            owner=request.user,
            box_type=PrivateMessageBox.BoxType.OUTBOX,
            is_read=True,
        )
        # Recipient's inbox entry
        PrivateMessageBox.objects.create(
            message=msg,
            owner=recipient,
            box_type=PrivateMessageBox.BoxType.INBOX,
            is_read=False,
        )

        return R.created({"message_id": msg.pk})


# ---------------------------------------------------------------------------
# POST /api/v1/conversations/{box_id}/messages  — reply
# ---------------------------------------------------------------------------

class ReplyPMView(APIView):
    """Reply to a PM (sends a new PM back to the original sender)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, box_id):
        box = get_object_or_404(
            PrivateMessageBox.objects.select_related("message__sender", "message__recipient"),
            pk=box_id,
            owner=request.user,
        )

        content = (request.data.get("content") or "").strip()
        if not content:
            return R.error("MISSING_FIELD", "Wymagane pole: content.")

        from board.forms import validate_pm_content
        repaired, _changes, errors = validate_pm_content(content)
        if errors:
            return R.error("VALIDATION_ERROR", "; ".join(errors))

        original = box.message
        # Reply goes to the other party
        if original.sender_id == request.user.pk:
            recipient = original.recipient
        else:
            recipient = original.sender

        if recipient is None:
            return R.error("USER_NOT_FOUND", "Odbiorca nie istnieje.", 404)

        subject = original.subject
        if not subject.startswith("Re: "):
            subject = f"Re: {subject}"

        content_compressed = zlib.compress(repaired.encode("utf-8"))

        msg = PrivateMessage.objects.create(
            sender=request.user,
            recipient=recipient,
            subject=subject,
            content_compressed=content_compressed,
        )
        PrivateMessageBox.objects.create(
            message=msg, owner=request.user,
            box_type=PrivateMessageBox.BoxType.OUTBOX, is_read=True,
        )
        PrivateMessageBox.objects.create(
            message=msg, owner=recipient,
            box_type=PrivateMessageBox.BoxType.INBOX, is_read=False,
        )

        return R.created({"message_id": msg.pk})
