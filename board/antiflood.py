"""
Anti-flood rate limiting for the forum engine.

Sliding window with progressive cooldown: the more posts a user writes
within a time window, the longer they must wait before the next one.
Old posts naturally fall out of the window, so the cooldown decreases
when the user is idle.

Counts: Post + ChecklistItem (both are new content a spammer can flood).
Edits are NOT counted.
Exempt: root account and admins (role >= ROLE_ADMIN).
"""

import math
from datetime import timedelta
from django.utils import timezone
from django.conf import settings


# ---------------------------------------------------------------------------
# Default configuration (override in Django settings via ANTIFLOOD_CONFIG)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Sliding window duration in seconds (5 hours)
    "window_seconds": 5 * 3600,

    # Cooldown formula:
    #   cooldown(n) = floor(A * sqrt(n-1) + B * (n-1))  minutes, for n >= 2
    #   cooldown(1) = 0  (first post is always free)
    # n = number of posts already in the window
    "coeff_sqrt": 1.5,     # A — gentle ramp-up at the start
    "coeff_linear": 0.18,  # B — sustained growth under pressure

    # Hard cap: max posts allowed in one window regardless of timing
    "max_posts_in_window": 30,

    # Cooldown bounds
    "min_cooldown_seconds": 0,
    "max_cooldown_seconds": 1800,  # 30 minutes
}


def get_config():
    config = DEFAULT_CONFIG.copy()
    config.update(getattr(settings, "ANTIFLOOD_CONFIG", {}))
    return config


# ---------------------------------------------------------------------------
# Core cooldown calculation
# ---------------------------------------------------------------------------

def compute_cooldown_minutes(n, config=None):
    """
    Cooldown in minutes required before the (n+1)-th post.
    n = posts already in window. Returns 0.0 for n <= 1.
    """
    if config is None:
        config = get_config()
    if n <= 1:
        return 0.0
    m = n - 1
    cooldown = config["coeff_sqrt"] * math.sqrt(m) + config["coeff_linear"] * m
    return max(0.0, math.floor(cooldown))


def compute_cooldown_seconds(n, config=None):
    return compute_cooldown_minutes(n, config) * 60


# ---------------------------------------------------------------------------
# Sliding window counting
# ---------------------------------------------------------------------------

def count_posts_in_window(user, config=None):
    """
    Count posts + checklist items the user created within the sliding window.
    Returns (total_count, latest_datetime_or_None).
    """
    if config is None:
        config = get_config()

    window_start = timezone.now() - timedelta(seconds=config["window_seconds"])

    from board.models import Post, ChecklistItem

    posts_qs = Post.objects.filter(author=user, created_at__gte=window_start)
    items_qs = ChecklistItem.objects.filter(author=user, created_at__gte=window_start)

    total = posts_qs.count() + items_qs.count()

    latest_post = posts_qs.order_by("-created_at").values_list("created_at", flat=True).first()
    latest_item = items_qs.order_by("-created_at").values_list("created_at", flat=True).first()

    if latest_post and latest_item:
        latest = max(latest_post, latest_item)
    else:
        latest = latest_post or latest_item

    return total, latest


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def check_can_post(user, config=None):
    """
    Returns dict:
        allowed (bool), wait_seconds (int), posts_in_window (int),
        cooldown_seconds (int), message (str)
    """
    if config is None:
        config = get_config()

    if is_user_exempt(user):
        return _build_result(allowed=True, wait=0, count=0, cooldown=0)

    post_count, latest_post_time = count_posts_in_window(user, config)

    if post_count >= config["max_posts_in_window"]:
        wait = _time_until_window_slot_frees(user, config)
        return _build_result(
            allowed=False, wait=wait, count=post_count, cooldown=0,
            message=f"Osiągnięto limit {config['max_posts_in_window']} wpisów "
                    f"w oknie czasowym. Poczekaj aż starsze wpisy wypadną z okna.",
        )

    if post_count == 0 or latest_post_time is None:
        return _build_result(allowed=True, wait=0, count=post_count, cooldown=0)

    cooldown_secs = _clamp_cooldown(compute_cooldown_seconds(post_count, config), config)
    elapsed = (timezone.now() - latest_post_time).total_seconds()
    remaining = cooldown_secs - elapsed

    if remaining <= 0:
        return _build_result(allowed=True, wait=0, count=post_count, cooldown=cooldown_secs)

    wait = int(math.ceil(remaining))
    return _build_result(allowed=False, wait=wait, count=post_count, cooldown=cooldown_secs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_user_exempt(user):
    """Root and admins bypass flood limits."""
    if getattr(user, "is_root", False):
        return True
    from board.models import User as BoardUser
    return getattr(user, "role", 0) >= BoardUser.ROLE_ADMIN


def _clamp_cooldown(cooldown_secs, config):
    lo = config.get("min_cooldown_seconds", 0)
    hi = config.get("max_cooldown_seconds", 1800)
    return max(lo, min(hi, cooldown_secs))


def _time_until_window_slot_frees(user, config):
    """When hard cap is hit: seconds until the oldest post leaves the window."""
    window_start = timezone.now() - timedelta(seconds=config["window_seconds"])

    from board.models import Post, ChecklistItem

    oldest_post = (
        Post.objects.filter(author=user, created_at__gte=window_start)
        .order_by("created_at").values_list("created_at", flat=True).first()
    )
    oldest_item = (
        ChecklistItem.objects.filter(author=user, created_at__gte=window_start)
        .order_by("created_at").values_list("created_at", flat=True).first()
    )

    candidates = [t for t in (oldest_post, oldest_item) if t is not None]
    if not candidates:
        return 0

    oldest = min(candidates)
    expires_at = oldest + timedelta(seconds=config["window_seconds"])
    return max(0, int(math.ceil((expires_at - timezone.now()).total_seconds())))


def _build_result(allowed, wait, count, cooldown, message=None):
    return {
        "allowed": allowed,
        "wait_seconds": wait,
        "posts_in_window": count,
        "cooldown_seconds": int(cooldown),
        "message": message or ("OK" if allowed else ""),
    }
