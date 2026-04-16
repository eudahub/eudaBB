import zlib

from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


AVATAR_MAX_BYTES  = 80 * 1024   # 80 kB
AVATAR_MAX_W      = 150
AVATAR_MAX_H      = 150
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
    username = models.CharField(max_length=31, unique=True)
    email = models.EmailField(blank=True, default="")

    signature = models.TextField(blank=True, default="")
    website = models.URLField(blank=True, default="")
    location = models.CharField(max_length=100, blank=True, default="")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True, validators=[validate_avatar])
    post_count = models.PositiveIntegerField(default=0)
    likes_given_count = models.PositiveIntegerField(default=0)
    likes_received_count = models.PositiveIntegerField(default=0)
    rank = models.CharField(max_length=64, blank=True, default="")
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
        max_length=31, blank=True, default="", unique=True,
        help_text="Lowercase, no diacritics, alphanumeric only. Used for uniqueness checks.",
    )

    banned_until = models.DateTimeField(null=True, blank=True)
    ban_reason = models.TextField(blank=True, default="")
    is_processing = models.BooleanField(
        default=False,
        help_text="Locked during rename/delete — blocks posting, quoting, editing.",
    )
    archive_access = models.SmallIntegerField(
        default=0,
        help_text="Max archive_level user can see: 0=normal, 1=soft darkweb, 2=hard darkweb (admin-granted)",
    )
    mark_all_read_at = models.DateTimeField(
        default=timezone.now,
        help_text="Global baseline: everything older than this is treated as read.",
    )
    is_root = models.BooleanField(
        default=False,
        help_text="Superadmin: manages users and forum structure. Cannot post, has no email, no password reset.",
    )
    is_temporary = models.BooleanField(
        default=False,
        help_text="Temporary account created during maintenance/beta — deleted on mode cleanup.",
    )
    registration_ip = models.GenericIPAddressField(
        null=True, blank=True,
        help_text="IP address used to register this account (for multi-account detection).",
    )

    ROLE_USER      = 0
    ROLE_MODERATOR = 1
    ROLE_ADMIN     = 2
    ROLE_CHOICES   = [(0, "Użytkownik"), (1, "Moderator"), (2, "Administrator")]

    role = models.SmallIntegerField(
        default=0,
        choices=ROLE_CHOICES,
        db_index=True,
        help_text="0=użytkownik, 1=moderator, 2=administrator. Root jest osobnym kontem (is_root).",
    )

    active_days = models.PositiveIntegerField(
        default=0,
        help_text="Number of distinct UTC days on which the user posted at least one post.",
    )

    def is_ghost(self) -> bool:
        """True when account has no usable password (archive-imported, never claimed)."""
        return not self.has_usable_password()

    def clean(self):
        from django.core.exceptions import ValidationError
        super().clean()
        if self.is_root and self.email:
            raise ValidationError({"email": "Konto root nie może mieć adresu email."})

    def save(self, *args, **kwargs):
        from .username_utils import normalize
        # Root must never have an email — enforce silently even if clean() was bypassed.
        if self.is_root:
            self.email = ""
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


class IgnoredUser(models.Model):
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ignored_users_rel"
    )
    ignored_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ignored_by_users_rel"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forum_ignored_users"
        unique_together = [("owner", "ignored_user")]
        indexes = [
            models.Index(fields=["owner"]),
            models.Index(fields=["ignored_user"]),
        ]

    def clean(self):
        if self.owner_id and self.owner_id == self.ignored_user_id:
            raise ValidationError("Nie można ignorować samego siebie.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"IgnoredUser owner={self.owner_id} ignored={self.ignored_user_id}"


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

    CODE_EXPIRY_HOURS = 4
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
        BLOGGER = 3, "Admin + Blogger"

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

    class Feature(models.IntegerChoices):
        PLAIN = 0, "Plain"
        POLL = 1, "Poll"
        CHECKLIST = 2, "Checklist"

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
    feature = models.IntegerField(
        choices=Feature.choices, default=Feature.PLAIN,
    )
    is_locked = models.BooleanField(default=False)
    is_temporary = models.BooleanField(
        default=False, db_index=True,
        help_text="Auto-managed: True when all posts are temporary, False when any post is permanent.",
    )
    is_pending = models.BooleanField(
        default=False, db_index=True,
        help_text="Topic awaiting moderation — not visible on the forum.",
    )
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

    open_report_count = models.PositiveSmallIntegerField(default=0, db_index=True)

    # Denormalized "last visible post" per spam class.
    # Updated whenever a post is added. Lets "Nowe wątki" / "Nowe posty"
    # filter without joining Post + User and without Exists() subqueries.
    # NORMAL user uses *_normal, GRAY user uses *_gray, WEB user uses last_post_at.
    last_post_at_normal = models.DateTimeField(null=True, blank=True, db_index=True)
    last_post_normal_author_id = models.IntegerField(null=True, blank=True)
    last_post_at_gray = models.DateTimeField(null=True, blank=True, db_index=True)
    last_post_gray_author_id = models.IntegerField(null=True, blank=True)

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

    is_temporary = models.BooleanField(
        default=False, db_index=True,
        help_text="Temporary post — excluded from export, deleted on mode cleanup.",
    )
    is_pending = models.BooleanField(
        default=False, db_index=True,
        help_text="Post awaiting moderation — not visible on the forum.",
    )
    has_open_report = models.BooleanField(default=False, db_index=True)

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


class MorphForm(models.Model):
    """Morfologiczne rodziny słów (PoliMorf) do ekspansji zapytań typu słowo+."""

    pk = models.CompositePrimaryKey("form_norm", "lemma_norm", "family_id")
    form_norm  = models.CharField(max_length=120)
    lemma_norm = models.CharField(max_length=120)
    family_id  = models.IntegerField()
    nom_form   = models.CharField(max_length=120, default="")

    class Meta:
        db_table = "forum_morph_form"
        indexes = [
            models.Index(fields=["form_norm"]),
            models.Index(fields=["lemma_norm", "family_id"]),
        ]


class MorphSuffix(models.Model):
    """Sufiksy lematów do analogii morfologicznej — fallback dla słów spoza MorphForm."""

    suffix_len = models.SmallIntegerField()
    suffix     = models.CharField(max_length=8)
    lemma_norm = models.CharField(max_length=120)
    family_id  = models.IntegerField()

    class Meta:
        db_table = "forum_morph_suffix"
        unique_together = [("suffix_len", "suffix", "lemma_norm", "family_id")]
        indexes = [
            models.Index(fields=["suffix_len", "suffix"]),
        ]


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

    def _has_votes(self):
        if self.total_votes:
            return True
        if not self.pk:
            return False
        return self.votes.exists()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = Poll.objects.get(pk=self.pk)
            if previous._has_votes():
                protected_fields = [
                    "topic_id",
                    "question",
                    "allow_vote_change",
                    "allow_multiple_choice",
                    "is_archived_import",
                ]
                for field_name in protected_fields:
                    if getattr(previous, field_name) != getattr(self, field_name):
                        raise ValidationError(
                            "Po oddaniu pierwszego głosu nie można już zmieniać pytania ani typu ankiety."
                        )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._has_votes():
            raise ValidationError("Po oddaniu pierwszego głosu nie można usunąć ankiety.")
        return super().delete(*args, **kwargs)


class PollOption(models.Model):
    """One option inside a poll."""

    poll = models.ForeignKey(
        Poll, on_delete=models.CASCADE, related_name="options"
    )
    option_text = models.TextField()
    category = models.CharField(max_length=200, default="", blank=True)
    vote_count = models.PositiveIntegerField(default=0)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "forum_poll_options"
        ordering = ["sort_order", "id"]
        unique_together = [("poll", "sort_order")]

    def __str__(self):
        return f"PollOption poll={self.poll_id} #{self.sort_order}"

    def save(self, *args, **kwargs):
        if self.pk:
            previous = PollOption.objects.select_related("poll").get(pk=self.pk)
            if previous.poll._has_votes():
                protected_fields = ["poll_id", "option_text", "sort_order"]
                for field_name in protected_fields:
                    if getattr(previous, field_name) != getattr(self, field_name):
                        raise ValidationError(
                            "Po oddaniu pierwszego głosu nie można już zmieniać odpowiedzi ankiety."
                        )
        elif self.poll._has_votes():
            raise ValidationError("Po oddaniu pierwszego głosu nie można już dodawać odpowiedzi ankiety.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.poll._has_votes():
            raise ValidationError("Po oddaniu pierwszego głosu nie można usunąć odpowiedzi ankiety.")
        return super().delete(*args, **kwargs)


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


# ---------------------------------------------------------------------------
# Checklist (interactive task list attached to a topic)
# ---------------------------------------------------------------------------

class Checklist(models.Model):
    """Interactive checklist attached 1:1 to a topic (like Poll)."""

    class DefaultSort(models.TextChoices):
        UPVOTES = "upvotes", "Upvotes"
        PRIORITY = "priority", "Priority"
        DATE = "date", "Date"
        STATUS = "status", "Status"

    topic = models.OneToOneField(
        Topic, on_delete=models.CASCADE, related_name="checklist"
    )
    allow_user_proposals = models.BooleanField(default=True)
    default_sort = models.CharField(
        max_length=10, choices=DefaultSort.choices, default=DefaultSort.UPVOTES,
    )
    allowed_tags = models.TextField(blank=True, default="")
    is_closed = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "forum_checklists"

    def __str__(self):
        return f"Checklist topic={self.topic_id}"


class ChecklistCategory(models.Model):
    """One category (tag) within a checklist."""

    checklist = models.ForeignKey(
        Checklist, on_delete=models.CASCADE, related_name="categories"
    )
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=7, default="#6c757d")  # hex
    order = models.IntegerField(default=0)

    class Meta:
        db_table = "forum_checklist_categories"
        ordering = ["order", "id"]
        unique_together = [("checklist", "name")]

    def __str__(self):
        return f"{self.name} (checklist={self.checklist_id})"


class ChecklistItem(models.Model):
    """One item (proposal / task) in a checklist."""

    class Status(models.IntegerChoices):
        PENDING = 0, "Pending"
        REJECTED = 1, "Rejected"
        NEW = 2, "New"
        IN_PROGRESS = 3, "In progress"
        DONE = 4, "Done"
        WONT_FIX = 5, "Won't fix"
        DUPLICATE = 6, "Duplicate"

    class Priority(models.IntegerChoices):
        CRITICAL = 1, "Critical"
        IMPORTANT = 2, "Important"
        MINOR = 3, "Minor"
        PLANNED = 4, "Planned"

    checklist = models.ForeignKey(
        Checklist, on_delete=models.CASCADE, related_name="items"
    )
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="checklist_items"
    )
    author_label = models.CharField(max_length=30, blank=True, default="")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    category = models.ForeignKey(
        ChecklistCategory, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="items"
    )
    status = models.IntegerField(
        choices=Status.choices, default=Status.NEW
    )
    priority = models.IntegerField(
        choices=Priority.choices, null=True, blank=True
    )
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="duplicates"
    )
    rejection_reason = models.CharField(max_length=500, blank=True, default="")
    tag = models.CharField(max_length=50, blank=True, default="")
    upvote_count = models.IntegerField(default=0)
    anon_upvote_count = models.IntegerField(default=0)
    comment_count = models.IntegerField(default=0)
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+"
    )

    class Meta:
        db_table = "forum_checklist_items"
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["checklist", "status"]),
            models.Index(fields=["checklist", "-upvote_count"]),
            models.Index(fields=["checklist", "created_at"]),
            models.Index(fields=["checklist", "order"]),
        ]

    def __str__(self):
        return f"ChecklistItem #{self.pk} '{self.title[:40]}'"

    @property
    def total_upvotes(self):
        return self.upvote_count + self.anon_upvote_count

    def display_author(self):
        if self.author is not None:
            return self.author.username
        return self.author_label or "Anonimus"


class ChecklistUpvote(models.Model):
    """One upvote per user per checklist item."""

    item = models.ForeignKey(
        ChecklistItem, on_delete=models.CASCADE, related_name="upvotes"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="checklist_upvotes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forum_checklist_upvotes"
        unique_together = [("item", "user")]
        indexes = [
            models.Index(fields=["item"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"ChecklistUpvote item={self.item_id} user={self.user_id}"


class ChecklistComment(models.Model):
    """Short comment under a checklist item."""

    item = models.ForeignKey(
        ChecklistItem, on_delete=models.CASCADE, related_name="comments"
    )
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="checklist_comments"
    )
    author_label = models.CharField(max_length=30, blank=True, default="")
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "forum_checklist_comments"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["item", "created_at"]),
        ]

    def __str__(self):
        return f"ChecklistComment #{self.pk} item={self.item_id}"

    def display_author(self):
        if self.author is not None:
            return self.author.username
        return self.author_label or "Anonimus"


class TopicParticipant(models.Model):
    """Per-topic participation counters for one user."""

    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="topic_participations"
    )
    post_count = models.PositiveIntegerField(default=0)
    last_post_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "forum_topic_participants"
        unique_together = [("topic", "user")]
        indexes = [
            models.Index(fields=["topic", "post_count"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"TopicParticipant topic={self.topic_id} user={self.user_id} posts={self.post_count}"


class TopicReadState(models.Model):
    """Per-user read progress for one topic."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="topic_read_states"
    )
    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE, related_name="read_states"
    )
    last_read_post_order = models.PositiveIntegerField(default=0)
    last_read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "forum_topic_read_states"
        unique_together = [("user", "topic")]
        indexes = [
            models.Index(fields=["user", "last_read_at"]),
            models.Index(fields=["topic"]),
        ]

    def __str__(self):
        return f"TopicReadState user={self.user_id} topic={self.topic_id} order={self.last_read_post_order}"


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

    search_snippet_chars = models.PositiveIntegerField(default=800)
    poll_options_soft_max = models.PositiveSmallIntegerField(
        default=50,
        help_text="Miękki limit opcji ankiety (max = twardy limit z settings POLL_OPTIONS_HARD_MAX, domyślnie 64).",
    )

    MODE_PRODUCTION  = "production"
    MODE_READONLY    = "readonly"
    MODE_MAINTENANCE = "maintenance"
    MODE_BETA        = "beta"
    SITE_MODE_CHOICES = [
        (MODE_PRODUCTION,  "Produkcja"),
        (MODE_READONLY,    "Tylko do odczytu"),
        (MODE_MAINTENANCE, "Serwis (bramka + tymczasowe konta)"),
        (MODE_BETA,        "Beta (otwarte + tymczasowe konta)"),
    ]
    site_mode = models.CharField(
        max_length=12, choices=SITE_MODE_CHOICES, default=MODE_PRODUCTION
    )
    maintenance_message = models.TextField(
        blank=True, default="",
        help_text="Komunikat wyświetlany podczas przerwy technicznej.",
    )
    reg_ip_limit = models.BooleanField(
        default=True,
        help_text="Włącz limit rejestracji z tego samego IP.",
    )
    reg_ip_window_hours = models.PositiveSmallIntegerField(
        default=6,
        help_text="Okno czasowe limitu rejestracji (godziny).",
    )
    reg_ip_max_real = models.PositiveSmallIntegerField(
        default=1,
        help_text="Max rejestracji realnych kont z jednego IP w oknie czasowym.",
    )
    reg_ip_max_temp = models.PositiveSmallIntegerField(
        default=3,
        help_text="Max rejestracji kont tymczasowych z jednego IP w oknie czasowym.",
    )

    # PM antiflood
    pm_min_active_days = models.PositiveSmallIntegerField(
        default=1,
        help_text="Minimalny active_days aby móc wysyłać PM (0 = brak bramki).",
    )
    pm_max_burst = models.PositiveSmallIntegerField(
        default=2,
        help_text="Max nieprzerwanych PM do tej samej osoby bez odpowiedzi.",
    )
    pm_cold_reset_hours = models.PositiveSmallIntegerField(
        default=24,
        help_text="Po ilu godzinach bez odpowiedzi licznik burst się resetuje.",
    )
    pm_new_recipients_per_day = models.PositiveSmallIntegerField(
        default=5,
        help_text="Max nowych rozmówców (bez historii) dziennie.",
    )

    class Meta:
        db_table = "forum_siteconfig"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "SiteConfig"


class Notification(models.Model):
    class Type(models.TextChoices):
        QUOTE_REPLY        = "quote_reply",        "Odpowiedź z cytatem"
        POST_LIKED         = "post_liked",          "Plus za post"
        POST_UNLIKED       = "post_unliked",        "Cofnięcie plusa"
        PENDING_QUEUE      = "pending_queue",       "Kolejka oczekujących"
        POST_REPORTED      = "post_reported",       "Zgłoszony post"
        PM_REPORTED        = "pm_reported",         "Zgłoszona PM"
        REPORT_CLOSED_POST = "report_closed_post",  "Zamknięto zgłoszenie postu"
        REPORT_CLOSED_PM   = "report_closed_pm",    "Zamknięto zgłoszenie PM"

    recipient  = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="notifications"
    )
    notif_type = models.CharField(max_length=24, choices=Type.choices)
    is_read    = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Who triggered this notification (null = system)
    actor = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sent_notifications",
    )
    # Relevant objects — only one filled depending on type
    post = models.ForeignKey(
        "Post", null=True, blank=True, on_delete=models.CASCADE,
        related_name="+",
    )
    pm = models.ForeignKey(
        "PrivateMessage", null=True, blank=True, on_delete=models.CASCADE,
        related_name="+",
    )

    class Meta:
        db_table = "forum_notification"
        ordering = ["-created_at"]
        indexes  = [models.Index(fields=["recipient", "is_read", "created_at"])]

    def __str__(self):
        return f"Notif({self.notif_type}) → {self.recipient_id}"


class PostReport(models.Model):
    # Web form uses these labels; API may send any free text.
    REASON_OFFTOP = "offtop"
    REASON_RULES  = "rules"
    WEB_REASON_CHOICES = [
        (REASON_OFFTOP, "Offtop"),
        (REASON_RULES,  "Łamie regulamin"),
    ]

    class Resolution(models.TextChoices):
        RESOLVED  = "resolved",  "Rozwiązane"
        DISMISSED = "dismissed", "Oddalone"

    post     = models.ForeignKey("Post",  on_delete=models.CASCADE, related_name="board_reports")
    reporter = models.ForeignKey(User,    on_delete=models.SET_NULL, null=True, related_name="submitted_reports")
    # Free-text: web sends "offtop"/"rules", Android may send anything (max 500)
    reason   = models.CharField(max_length=500, blank=True, default="")
    comment  = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    resolved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="resolved_reports",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    is_closed   = models.BooleanField(default=False, db_index=True)
    # How it was closed: "resolved" (action taken) or "dismissed" (no action)
    resolution  = models.CharField(
        max_length=10, choices=Resolution.choices, blank=True, default=""
    )

    class Meta:
        db_table      = "forum_post_report"
        unique_together = [("post", "reporter")]
        ordering      = ["-created_at"]

    @property
    def status(self):
        """API-compatible status string: open / resolved / dismissed."""
        if not self.is_closed:
            return "open"
        return self.resolution or "resolved"

    def __str__(self):
        return f"Report(post={self.post_id}, reporter={self.reporter_id}, status={self.status})"


class UserSession(models.Model):
    """Tracks active sessions per user for concurrent-login detection."""
    user = models.ForeignKey(
        "User", on_delete=models.CASCADE, related_name="active_sessions"
    )
    session_key = models.CharField(max_length=40, unique=True)
    ip_address = models.GenericIPAddressField()
    last_seen = models.DateTimeField()

    class Meta:
        db_table = "forum_user_session"
        indexes = [models.Index(fields=["user", "last_seen"])]

    def __str__(self):
        return f"{self.user_id}@{self.ip_address}"


class MaintenanceAllowedUser(models.Model):
    """Users allowed to log in during closed maintenance mode."""
    username = models.CharField(max_length=31, unique=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forum_maintenance_allowed_user"

    def __str__(self):
        return self.username


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


class SpamDomain(models.Model):
    domain = models.CharField(max_length=255, primary_key=True)
    spam = models.SmallIntegerField()
    added_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "forum_spam_domain"


class BlockedCountry(models.Model):
    """Countries from which registration and posting are blocked."""
    country_code = models.CharField(max_length=2, primary_key=True)
    country_name = models.CharField(max_length=100, blank=True, default="")
    blocked_by = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    blocked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "board_blocked_countries"
        ordering = ["country_code"]

    def __str__(self):
        return f"{self.country_code} ({self.country_name})" if self.country_name else self.country_code


class SpamEmail(models.Model):
    """Individual email addresses permanently banned (e.g. from released spammer accounts)."""
    email = models.EmailField(max_length=254, primary_key=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forum_spam_email"


class ModerationWindow(models.Model):
    """Time window during which posts by new users (active_days<=5) go to the moderation queue."""

    DAY_NAMES = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]

    start_hour = models.PositiveSmallIntegerField()
    start_minute = models.PositiveSmallIntegerField(default=0)
    end_hour = models.PositiveSmallIntegerField()
    end_minute = models.PositiveSmallIntegerField(default=0)
    # 0=Mon … 6=Sun; null = all days
    day_from = models.PositiveSmallIntegerField(null=True, blank=True)
    day_to = models.PositiveSmallIntegerField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/Warsaw")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        "User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="moderation_windows",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "board_moderation_windows"
        ordering = ["start_hour", "start_minute"]

    @property
    def day_range_label(self):
        if self.day_from is not None and self.day_to is not None:
            return f"{self.DAY_NAMES[self.day_from]}–{self.DAY_NAMES[self.day_to]}"
        return "każdy dzień"

    def __str__(self):
        days = ""
        if self.day_from is not None and self.day_to is not None:
            days = f" ({self.DAY_NAMES[self.day_from]}–{self.DAY_NAMES[self.day_to]})"
        return (
            f"{self.start_hour:02d}:{self.start_minute:02d}"
            f"–{self.end_hour:02d}:{self.end_minute:02d}{days}"
            f" [{self.timezone}]"
        )
