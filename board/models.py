from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.auth.models import AbstractUser
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

    class Meta:
        db_table = "forum_siteconfig"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "SiteConfig"
