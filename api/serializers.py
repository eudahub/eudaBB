"""API serializers.

Read-only serializers return data for GET responses.
Write serializers validate incoming POST/PUT data.
"""

import zlib
from rest_framework import serializers
from board.models import (
    User, Section, Forum, Topic, Post,
    PrivateMessage, PrivateMessageBox,
    PostReport, Notification,
)
from board.bbcode import render as bbcode_render


# ---------------------------------------------------------------------------
# Auth / user
# ---------------------------------------------------------------------------

def _role_string(user) -> str:
    if user.is_root:
        return "root"
    if user.role >= User.ROLE_ADMIN:
        return "admin"
    if user.role >= User.ROLE_MODERATOR:
        return "moderator"
    return "user"


class UserBriefSerializer(serializers.ModelSerializer):
    """Minimal user data embedded in posts, topics, etc."""
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "avatar_url"]

    def get_avatar_url(self, obj):
        if obj.avatar:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.avatar.url)
            return obj.avatar.url
        return None


class UserProfileSerializer(serializers.ModelSerializer):
    """Full user profile for GET /api/v1/users/{id}/profile."""
    role = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    signature_html = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "role", "post_count",
            "likes_received_count", "date_joined",
            "rank", "location", "website",
            "avatar_url", "signature_html",
        ]

    def get_role(self, obj):
        return _role_string(obj)

    def get_avatar_url(self, obj):
        if obj.avatar:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.avatar.url)
            return obj.avatar.url
        return None

    def get_signature_html(self, obj):
        if obj.signature:
            return bbcode_render(obj.signature)
        return ""


class MeSerializer(serializers.ModelSerializer):
    """Logged-in user's own data returned in token response."""
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "role", "post_count", "date_joined"]

    def get_role(self, obj):
        return _role_string(obj)


# ---------------------------------------------------------------------------
# Forum structure
# ---------------------------------------------------------------------------

class ForumBriefSerializer(serializers.ModelSerializer):
    last_post_at = serializers.DateTimeField()

    class Meta:
        model = Forum
        fields = [
            "id", "title", "description", "order",
            "topic_count", "post_count", "last_post_at",
        ]


class SectionWithForumsSerializer(serializers.ModelSerializer):
    """Section with its top-level forums. Used for GET /api/v1/categories."""
    forums = serializers.SerializerMethodField()

    class Meta:
        model = Section
        fields = ["id", "title", "order", "forums"]

    def get_forums(self, obj):
        # Only top-level forums (no parent) in this section, respecting access level
        request = self.context.get("request")
        user = request.user if request else None
        qs = obj.forums.filter(parent=None).order_by("order")
        qs = _filter_forums_by_access(qs, user)
        return ForumWithSubsSerializer(qs, many=True, context=self.context).data


class ForumWithSubsSerializer(serializers.ModelSerializer):
    """Forum with subforums. Used inside SectionWithForumsSerializer."""
    subforums = serializers.SerializerMethodField()
    last_post_at = serializers.DateTimeField()

    class Meta:
        model = Forum
        fields = [
            "id", "title", "description", "order",
            "topic_count", "post_count", "last_post_at",
            "subforums",
        ]

    def get_subforums(self, obj):
        request = self.context.get("request")
        user = request.user if request else None
        qs = obj.subforums.order_by("order")
        qs = _filter_forums_by_access(qs, user)
        return ForumBriefSerializer(qs, many=True, context=self.context).data


def _filter_forums_by_access(qs, user):
    from board.models import Forum as F
    if user and user.is_authenticated and (user.is_root or user.role >= User.ROLE_ADMIN):
        return qs  # admins see everything
    if user and user.is_authenticated:
        return qs.filter(access_level__lte=F.AccessLevel.REGISTERED)
    return qs.filter(access_level=F.AccessLevel.PUBLIC)


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

class TopicListSerializer(serializers.ModelSerializer):
    """Topic row for thread list."""
    author_id = serializers.IntegerField(source="author.id", default=None)
    author_name = serializers.CharField(source="author.username", default=None)
    is_pinned = serializers.SerializerMethodField()
    post_count = serializers.IntegerField(source="reply_count")

    class Meta:
        model = Topic
        fields = [
            "id", "title", "author_id", "author_name",
            "topic_type", "is_pinned", "is_locked", "feature",
            "post_count", "view_count",
            "created_at", "last_post_at",
        ]

    def get_is_pinned(self, obj):
        return obj.topic_type >= Topic.TopicType.STICKY


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

class PostSerializer(serializers.ModelSerializer):
    """Full post for thread view."""
    author_id = serializers.IntegerField(source="author.id", default=None)
    author_name = serializers.CharField(source="author.username", default=None)
    author_avatar_url = serializers.SerializerMethodField()
    content_html = serializers.SerializerMethodField()
    edited_at = serializers.DateTimeField(source="updated_at")

    class Meta:
        model = Post
        fields = [
            "id", "author_id", "author_name", "author_avatar_url",
            "content_bbcode", "content_html",
            "created_at", "edited_at", "edit_count",
            "like_count", "post_order", "is_temporary",
        ]

    def get_author_avatar_url(self, obj):
        if obj.author and obj.author.avatar:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.author.avatar.url)
            return obj.author.avatar.url
        return None

    def get_content_html(self, obj):
        if obj.broken_tags:
            # Tags unbalanced on import — return raw, escaped
            from django.utils.html import escape
            return f"<pre>{escape(obj.content_bbcode)}</pre>"
        return bbcode_render(obj.content_bbcode)


class PostWriteSerializer(serializers.Serializer):
    """Validates incoming post content."""
    content = serializers.CharField(min_length=1)

    def validate_content(self, value):
        from board.forms import validate_post_content
        repaired, _changes, errors = validate_post_content(value)
        if errors:
            raise serializers.ValidationError(errors)
        return repaired


class NewTopicWriteSerializer(serializers.Serializer):
    title = serializers.CharField(min_length=1, max_length=70)
    content = serializers.CharField(min_length=1)

    def validate_content(self, value):
        from board.forms import validate_post_content
        repaired, _changes, errors = validate_post_content(value)
        if errors:
            raise serializers.ValidationError(errors)
        return repaired


# ---------------------------------------------------------------------------
# Private Messages
# ---------------------------------------------------------------------------

class PMBoxSerializer(serializers.ModelSerializer):
    """Inbox/outbox item for conversation list."""
    other_user_id = serializers.SerializerMethodField()
    other_user_name = serializers.SerializerMethodField()
    subject = serializers.CharField(source="message.subject")
    created_at = serializers.DateTimeField(source="message.created_at")
    is_read = serializers.BooleanField()

    class Meta:
        model = PrivateMessageBox
        fields = [
            "id", "other_user_id", "other_user_name",
            "subject", "created_at", "is_read", "box_type",
        ]

    def get_other_user_id(self, obj):
        request = self.context.get("request")
        me = request.user if request else None
        msg = obj.message
        if me and msg.sender_id == me.id:
            return msg.recipient_id
        return msg.sender_id

    def get_other_user_name(self, obj):
        request = self.context.get("request")
        me = request.user if request else None
        msg = obj.message
        if me and msg.sender_id == me.id:
            return msg.recipient.username if msg.recipient else "[usunięty]"
        return msg.sender.username if msg.sender else "[usunięty]"


class PMDetailSerializer(serializers.ModelSerializer):
    """Full PM message content."""
    sender_id = serializers.IntegerField(source="message.sender_id")
    sender_name = serializers.SerializerMethodField()
    recipient_id = serializers.IntegerField(source="message.recipient_id")
    recipient_name = serializers.SerializerMethodField()
    subject = serializers.CharField(source="message.subject")
    content = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source="message.created_at")
    is_read = serializers.BooleanField()

    class Meta:
        model = PrivateMessageBox
        fields = [
            "id", "sender_id", "sender_name",
            "recipient_id", "recipient_name",
            "subject", "content", "created_at", "is_read", "box_type",
        ]

    def get_sender_name(self, obj):
        s = obj.message.sender
        return s.username if s else "[usunięty]"

    def get_recipient_name(self, obj):
        r = obj.message.recipient
        return r.username if r else "[usunięty]"

    def get_content(self, obj):
        try:
            raw = zlib.decompress(bytes(obj.message.content_compressed))
            return raw.decode("utf-8")
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class PostReportSerializer(serializers.ModelSerializer):
    reporter_name = serializers.CharField(source="reporter.username", default=None)
    post_topic_id = serializers.IntegerField(source="post.topic_id", default=None)
    # Map is_closed + resolution → status string for API compatibility
    status = serializers.SerializerMethodField()

    class Meta:
        model = PostReport
        fields = [
            "id", "post_id", "post_topic_id",
            "reporter_id", "reporter_name",
            "reason", "comment", "status", "created_at",
        ]

    def get_status(self, obj):
        return obj.status


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotificationSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.username", default=None)
    # Target object identifiers — only one will be non-null per notification
    post_id    = serializers.IntegerField(source="post.pk",         default=None)
    topic_id   = serializers.IntegerField(source="post.topic_id",   default=None)
    pm_id      = serializers.IntegerField(source="pm.pk",           default=None)

    class Meta:
        model  = Notification
        fields = [
            "id", "notif_type", "is_read", "created_at",
            "actor_id", "actor_name",
            "post_id", "topic_id", "pm_id",
        ]
