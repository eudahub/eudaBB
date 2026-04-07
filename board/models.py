from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


AVATAR_MAX_BYTES  = 64 * 1024   # 64 kB
AVATAR_MAX_W      = 128
AVATAR_MAX_H      = 128
AVATAR_MIN_W      = 16
AVATAR_MIN_H      = 32


def validate_avatar(image):
    if image.size > AVATAR_MAX_BYTES:
        raise ValidationError(
            f"Plik za duży: {image.size // 1024} kB (max {AVATAR_MAX_BYTES // 1024} kB)."
        )
    if image.width > AVATAR_MAX_W or image.height > AVATAR_MAX_H:
        raise ValidationError(
            f"Wymiary {image.width}×{image.height} przekraczają max {AVATAR_MAX_W}×{AVATAR_MAX_H} px."
        )
    if image.width < AVATAR_MIN_W or image.height < AVATAR_MIN_H:
        raise ValidationError(
            f"Wymiary {image.width}×{image.height} poniżej min {AVATAR_MIN_W}×{AVATAR_MIN_H} px."
        )


class User(AbstractUser):
    """Forum user extending Django's AbstractUser.

    Email is never stored in plaintext. AbstractUser.email is kept blank always.
    Use email_hash (Argon2) for verification and email_mask for display.
    """
    email = models.EmailField(blank=True, default="")

    signature = models.TextField(blank=True, default="")
    website = models.URLField(blank=True, default="")
    location = models.CharField(max_length=100, blank=True, default="")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True, validators=[validate_avatar])
    post_count = models.PositiveIntegerField(default=0)
    likes_given_count = models.PositiveIntegerField(default=0)
    likes_received_count = models.PositiveIntegerField(default=0)
    rank = models.CharField(max_length=64, blank=True, default="")
    is_ghost = models.BooleanField(
        default=False,
        help_text="Archive-imported account — no password, cannot log in",
    )
    class SpamClass(models.IntegerChoices):
        NORMAL = 0, "Normalny"
        GRAY   = 1, "Gray (zaśmiecacz)"
        WEB    = 2, "Web (bot/spam rejestracyjny)"

    spam_class = models.SmallIntegerField(
        choices=SpamClass.choices,
        default=SpamClass.NORMAL,
        db_index=True,
        help_text="Klasa spamu: 0=normalny, 1=gray, 2=web. Używana przez PLONK do filtrowania.",
    )

    username_normalized = models.CharField(
        max_length=150, blank=True, default="", db_index=True,
        help_text="Lowercase, no diacritics, alphanumeric only. Used for uniqueness checks.",
    )

    is_banned = models.BooleanField(default=False)
    ban_reason = models.TextField(blank=True, default="")
    archive_access = models.SmallIntegerField(
        default=0,
        help_text="Max archive_level user can see: 0=normal, 1=soft darkweb, 2=hard darkweb (admin-granted)",
    )
    is_root = models.BooleanField(
        default=False,
        help_text="Superadmin: manages users and forum structure. Cannot post, has no email, no password reset.",
    )

    def save(self, *args, **kwargs):
        from .username_utils import normalize
        self.username_normalized = normalize(self.username)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "username" in update_fields:
            kwargs["update_fields"] = list(set(update_fields) | {"username_normalized"})
        super().save(*args, **kwargs)

    class Meta:
        db_table = "forum_users"
        constraints = [
            models.UniqueConstraint(
                fields=["is_root"],
                condition=models.Q(is_root=True),
                name="only_one_root",
            )
        ]


class PrivateMessage(models.Model):
    """A PM stored once; delivery tracked via PrivateMessageBox entries."""
    sender    = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="sent_pms")
    recipient = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="received_pms")
    subject   = models.CharField(max_length=255)
    # zlib-compressed UTF-8 BBCode — readable only after zlib.decompress()
    content_compressed = models.BinaryField()
    created_at   = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True,
        help_text="Set when recipient visits inbox. Null = still in sender's outbox.")

    class Meta:
        db_table = "forum_pms"
        ordering = ["-created_at"]

    def __str__(self):
        return f"PM #{self.pk}: {self.subject}"


class PrivateMessageBox(models.Model):
    """One row per user-side of a message (sender or recipient)."""
    class BoxType(models.TextChoices):
        OUTBOX = "OUTBOX", "Outbox"
        SENT   = "SENT",   "Sent"
        INBOX  = "INBOX",  "Inbox"

    message  = models.ForeignKey(PrivateMessage, on_delete=models.CASCADE, related_name="boxes")
    owner    = models.ForeignKey(User, on_delete=models.CASCADE, related_name="pm_boxes")
    box_type = models.CharField(max_length=6, choices=BoxType.choices)
    is_read  = models.BooleanField(default=False)

    class Meta:
        db_table = "forum_pm_boxes"
        unique_together = [("message", "owner")]
        indexes = [
            models.Index(fields=["owner", "box_type"]),
        ]

    def __str__(self):
        return f"{self.owner} / {self.box_type} / PM#{self.message_id}"


class PasswordResetCode(models.Model):
    """6-digit numeric code for password reset (forgot password or invalidated password).

    Flow:
    - Up to MAX_PER_HOUR codes may be sent per hour (rate limit).
    - Each new code does NOT immediately invalidate the previous one.
    - The previous code remains valid for GRACE_MINUTES after it was created,
      to handle delayed mail delivery (user requests a second code impatiently).
    - A code becomes permanently invalid when: used, expired (EXPIRY_HOURS), or
      the grace window has elapsed and a newer code exists.
    """
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reset_codes")
    code       = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used    = models.BooleanField(default=False)

    CODE_EXPIRY_HOURS = 24
    GRACE_MINUTES     = 7
    MAX_PER_HOUR      = 3

    class Meta:
        db_table = "forum_password_reset_codes"
        ordering = ["-created_at"]

    def is_expired(self):
        return timezone.now() >= self.expires_at


class ActivationToken(models.Model):
    """Short-lived token for ghost account activation via email link."""
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="activation_token",
    )
    token = models.CharField(max_length=128, unique=True)
    expires_at = models.DateTimeField()

    # Rate limiting: max 10 failed email attempts, window resets after 1 hour
    failed_attempts = models.SmallIntegerField(default=0)
    window_start = models.DateTimeField(null=True, blank=True)

    MAX_ATTEMPTS = 10
    WINDOW_MINUTES = 60

    class Meta:
        db_table = "forum_activation_tokens"

    def is_valid(self):
        from django.utils import timezone
        return timezone.now() < self.expires_at

    def is_rate_limited(self):
        from django.utils import timezone
        from datetime import timedelta
        if not self.window_start:
            return False
        if timezone.now() > self.window_start + timedelta(minutes=self.WINDOW_MINUTES):
            # Window expired — reset
            self.failed_attempts = 0
            self.window_start = None
            self.save(update_fields=["failed_attempts", "window_start"])
            return False
        return self.failed_attempts >= self.MAX_ATTEMPTS

    def record_failed_attempt(self):
        from django.utils import timezone
        from datetime import timedelta
        now = timezone.now()
        if not self.window_start or now > self.window_start + timedelta(minutes=self.WINDOW_MINUTES):
            self.window_start = now
            self.failed_attempts = 0
        self.failed_attempts += 1
        self.save(update_fields=["failed_attempts", "window_start"])


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
    archive_topic_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True
    )
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


class BlockedIP(models.Model):
    """Admin-managed IP blocklist for proxies, VPNs, abusers etc.

    Unlike TorExitNode (auto-fetched), this list is maintained manually by root.
    An IP here blocks login and registration, same as TOR IPs.
    Bans are permanent until manually removed.
    """
    ip_address = models.GenericIPAddressField(unique=True, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    added_by = models.ForeignKey(
        "User", on_delete=models.SET_NULL,
        null=True, related_name="+",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forum_blocked_ips"
        ordering = ["-added_at"]

    def __str__(self):
        return self.ip_address


class TorExitNode(models.Model):
    """Cached list of TOR exit node IP addresses, refreshed hourly."""
    ip_address = models.GenericIPAddressField(unique=True, db_index=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "forum_tor_exit_nodes"

    def __str__(self):
        return self.ip_address


class Post(models.Model):
    """A single post within a topic."""
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="posts")
    archive_post_id = models.PositiveIntegerField(
        null=True, blank=True, db_index=True
    )
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, related_name="posts",
    )
    subject = models.CharField(max_length=255, blank=True, default="")

    # BBCode stored as-is; HTML is a render cache rebuilt on save
    content_bbcode = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="edited_posts",
    )
    edit_count = models.PositiveSmallIntegerField(default=0)

    # IP address of author — retained for law enforcement requests.
    # Automatically nulled after ip_retain_until by: manage.py purge_expired_ips
    author_ip = models.GenericIPAddressField(null=True, blank=True)
    ip_retain_until = models.DateTimeField(
        null=True, blank=True,
        help_text="IP kasowane po tej dacie. Ustawiane przy tworzeniu posta.",
    )
    ip_flagged = models.BooleanField(
        default=False,
        help_text="Moderator oznaczył post jako groźny — dłuższa retencja IP.",
    )

    # Sequential position within topic — used for pagination and quote references
    post_order = models.PositiveIntegerField(default=0)
    like_count = models.PositiveIntegerField(default=0)

    # Set during import when the post has unbalanced [quote]/[/quote] tags
    # (quote_status=4 in sfiniabb.db). Content is displayed verbatim, not parsed.
    broken_tags = models.BooleanField(default=False)

    class Meta:
        ordering = ["post_order"]
        db_table = "forum_posts"
        indexes = [
            models.Index(fields=["topic", "post_order"]),
        ]

    def __str__(self):
        return f"Post #{self.post_order} in '{self.topic}'"


class QuoteReference(models.Model):
    """Index of quote/fquote tags found in a post's BBCode."""

    class QuoteType(models.TextChoices):
        QUOTE = "quote", "Quote"
        FQUOTE = "fquote", "Foreign quote"

    post = models.ForeignKey(
        Post, on_delete=models.CASCADE, related_name="quote_references"
    )
    source_post = models.ForeignKey(
        Post, on_delete=models.SET_NULL, null=True, blank=True, related_name="quoted_by"
    )
    quote_type = models.CharField(max_length=6, choices=QuoteType.choices)
    quoted_username = models.TextField(blank=True, default="")
    depth = models.PositiveSmallIntegerField(default=1)
    ellipsis_count = models.PositiveSmallIntegerField(default=0)
    quote_index = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "forum_quote_refs"
        unique_together = [("post", "quote_index")]
        indexes = [
            models.Index(fields=["source_post"]),
            models.Index(fields=["quoted_username"]),
            models.Index(fields=["post", "depth"]),
        ]

    def __str__(self):
        return f"QuoteRef post={self.post_id} idx={self.quote_index}"


class PostSearchIndex(models.Model):
    """Materialized search source for one post (1:1 with Post)."""

    post = models.OneToOneField(
        Post, on_delete=models.CASCADE, related_name="search_index"
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE, related_name="search_posts"
    )
    forum = models.ForeignKey(
        Forum, on_delete=models.CASCADE, related_name="search_posts"
    )
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="search_posts"
    )
    created_at = models.DateTimeField()
    has_link = models.BooleanField(default=False)
    has_youtube = models.BooleanField(default=False)
    content_search_author = models.TextField(blank=True, default="")
    content_search_author_normalized = models.TextField(blank=True, default="")

    class Meta:
        db_table = "forum_post_search"
        indexes = [
            models.Index(fields=["forum", "created_at"]),
            models.Index(fields=["topic", "created_at"]),
            models.Index(fields=["author"]),
            models.Index(fields=["has_link"]),
            models.Index(fields=["has_youtube"]),
        ]

    def __str__(self):
        return f"PostSearch post={self.post_id}"


class Poll(models.Model):
    """Thread poll, both imported archival polls and native future polls."""

    topic = models.OneToOneField(
        Topic, on_delete=models.CASCADE, related_name="poll"
    )
    question = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)
    allow_vote_change = models.BooleanField(default=False)
    allow_multiple_choice = models.BooleanField(default=False)
    is_archived_import = models.BooleanField(default=False)
    total_votes = models.PositiveIntegerField(default=0)
    source_visibility = models.IntegerField(default=0)
    imported_results_text = models.TextField(blank=True, default="")
    imported_fetched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "forum_polls"
        indexes = [
            models.Index(fields=["is_closed", "ends_at"]),
            models.Index(fields=["is_archived_import"]),
        ]

    def __str__(self):
        return f"Poll topic={self.topic_id}"


class PollOption(models.Model):
    """One option inside a poll."""

    poll = models.ForeignKey(
        Poll, on_delete=models.CASCADE, related_name="options"
    )
    option_text = models.TextField()
    vote_count = models.PositiveIntegerField(default=0)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "forum_poll_options"
        ordering = ["sort_order", "id"]
        unique_together = [("poll", "sort_order")]

    def __str__(self):
        return f"PollOption poll={self.poll_id} #{self.sort_order}"


class PollVote(models.Model):
    """Per-user vote record for native polls.

    Imported archival polls do not populate this table because the archive does
    not contain per-user voting data.
    """

    poll = models.ForeignKey(
        Poll, on_delete=models.CASCADE, related_name="votes"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="poll_votes"
    )
    option = models.ForeignKey(
        PollOption, on_delete=models.CASCADE, related_name="votes"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "forum_poll_votes"
        indexes = [
            models.Index(fields=["poll", "user"]),
            models.Index(fields=["option"]),
        ]
        unique_together = [("poll", "user", "option")]

    def __str__(self):
        return f"PollVote poll={self.poll_id} user={self.user_id} option={self.option_id}"


class PostLike(models.Model):
    """One like per user per post."""

    post = models.ForeignKey(
        Post, on_delete=models.CASCADE, related_name="likes"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="given_post_likes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forum_post_likes"
        unique_together = [("post", "user")]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["post", "created_at"]),
        ]

    def __str__(self):
        return f"PostLike post={self.post_id} user={self.user_id}"


class SiteConfig(models.Model):
    """Singleton table (always pk=1) — site-wide toggles configurable by root."""

    # Reset code delivery: 'email' sends real mail, 'popup' shows code on screen
    RESET_EMAIL = "email"
    RESET_POPUP = "popup"
    RESET_MODE_CHOICES = [
        (RESET_EMAIL, "Wyślij emailem"),
        (RESET_POPUP, "Pokaż w oknie (tryb testowy)"),
    ]
    reset_mode = models.CharField(
        max_length=10, choices=RESET_MODE_CHOICES, default=RESET_EMAIL
    )

    # Show "Przełącz" link in nav (lets you quickly switch accounts — test use only)
    show_switch_link = models.BooleanField(default=False)
    search_snippet_chars = models.PositiveIntegerField(default=800)

    class Meta:
        db_table = "forum_siteconfig"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "SiteConfig"


@receiver(post_save, sender=Post)
def _sync_post_search_index(sender, instance, raw, **kwargs):
    if raw:
        return
    from .search_index import update_post_search_index
    update_post_search_index(instance)


@receiver(post_save, sender=PostLike)
def _increment_like_counters(sender, instance, created, **kwargs):
    if not created:
        return
    Post.objects.filter(pk=instance.post_id).update(
        like_count=models.F("like_count") + 1
    )
    User.objects.filter(pk=instance.user_id).update(
        likes_given_count=models.F("likes_given_count") + 1
    )
    if instance.post.author_id:
        User.objects.filter(pk=instance.post.author_id).update(
            likes_received_count=models.F("likes_received_count") + 1
        )


@receiver(models.signals.post_delete, sender=PostLike)
def _decrement_like_counters(sender, instance, **kwargs):
    Post.objects.filter(pk=instance.post_id, like_count__gt=0).update(
        like_count=models.F("like_count") - 1
    )
    User.objects.filter(pk=instance.user_id, likes_given_count__gt=0).update(
        likes_given_count=models.F("likes_given_count") - 1
    )
    if instance.post.author_id:
        User.objects.filter(pk=instance.post.author_id, likes_received_count__gt=0).update(
            likes_received_count=models.F("likes_received_count") - 1
        )
