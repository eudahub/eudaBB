from django.db import models
from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Forum user extending Django's AbstractUser."""
    signature = models.TextField(blank=True, default="")
    website = models.URLField(blank=True, default="")
    location = models.CharField(max_length=100, blank=True, default="")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    post_count = models.PositiveIntegerField(default=0)
    rank = models.CharField(max_length=64, blank=True, default="")
    is_ghost = models.BooleanField(
        default=False,
        help_text="Archive-imported account — no password, cannot log in",
    )
    is_banned = models.BooleanField(default=False)
    ban_reason = models.TextField(blank=True, default="")
    archive_access = models.SmallIntegerField(
        default=0,
        help_text="Max archive_level user can see: 0=normal, 1=soft darkweb, 2=hard darkweb (admin-granted)",
    )

    class Meta:
        db_table = "forum_users"


class Section(models.Model):
    """Top-level grouping of forums (phpBB: category)."""
    title = models.CharField(max_length=255)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        db_table = "forum_sections"

    def __str__(self):
        return self.title


class Forum(models.Model):
    """A forum board, optionally nested under a parent forum."""
    section = models.ForeignKey(
        Section, on_delete=models.PROTECT,
        related_name="forums",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="subforums",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    order = models.PositiveSmallIntegerField(default=0)

    class AccessLevel(models.IntegerChoices):
        PUBLIC = 0, "Public"
        REGISTERED = 1, "Registered only"
        ADMIN = 2, "Admin only"

    access_level = models.SmallIntegerField(
        choices=AccessLevel.choices,
        default=AccessLevel.PUBLIC,
        help_text="Who can see this forum (phpBB visibility_class)",
    )

    # Archive darkweb level — separate from access_level
    class ArchiveLevel(models.IntegerChoices):
        NORMAL = 0, "Normal"
        SOFT = 1, "Soft darkweb (spam=1 users)"
        HARD = 2, "Hard darkweb (Krowa clones, Więzienie etc.)"

    archive_level = models.SmallIntegerField(
        choices=ArchiveLevel.choices,
        default=ArchiveLevel.NORMAL,
        help_text="Archive content sensitivity level",
    )

    # Cached counters — updated by helpers in views.py
    topic_count = models.PositiveIntegerField(default=0)
    post_count = models.PositiveIntegerField(default=0)

    # Cached last post info — denormalized for performance (like phpBB)
    last_post = models.ForeignKey(
        "Post", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    last_post_at = models.DateTimeField(null=True, blank=True)

    moderators = models.ManyToManyField(User, blank=True, related_name="moderated_forums")

    class Meta:
        ordering = ["order"]
        db_table = "forum_forums"

    def __str__(self):
        return self.title


class Topic(models.Model):
    """A thread within a forum."""

    class TopicType(models.IntegerChoices):
        NORMAL = 0, "Normal"
        STICKY = 1, "Sticky"
        ANNOUNCEMENT = 2, "Announcement"

    forum = models.ForeignKey(Forum, on_delete=models.CASCADE, related_name="topics")
    title = models.CharField(max_length=255)
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, related_name="topics",
    )
    topic_type = models.IntegerField(
        choices=TopicType.choices, default=TopicType.NORMAL,
    )
    is_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Cached counters — updated by helpers in views.py
    reply_count = models.PositiveIntegerField(default=0)
    view_count = models.PositiveIntegerField(default=0)

    # Cached last post info — avoids JOIN on topic list
    last_post = models.ForeignKey(
        "Post", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    last_post_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # Announcements > Stickies > Normal, then newest last post first
        ordering = ["-topic_type", "-last_post_at"]
        db_table = "forum_topics"
        indexes = [
            models.Index(fields=["forum", "-topic_type", "-last_post_at"]),
        ]

    def __str__(self):
        return self.title


class Post(models.Model):
    """A single post within a topic."""
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="posts")
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, related_name="posts",
    )
    subject = models.CharField(max_length=255, blank=True, default="")

    # BBCode stored as-is; HTML is a render cache rebuilt on save
    content_bbcode = models.TextField()
    content_html = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="edited_posts",
    )
    edit_count = models.PositiveSmallIntegerField(default=0)

    # Sequential position within topic — used for pagination and quote references
    post_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["post_order"]
        db_table = "forum_posts"
        indexes = [
            models.Index(fields=["topic", "post_order"]),
        ]

    def __str__(self):
        return f"Post #{self.post_order} in '{self.topic}'"
