"""
Anti-flood rate limiting module for forum engine.

Uses a sliding window approach with progressive cooldown.
The more posts a user writes within a time window, the longer
they must wait before posting again. The window naturally
"forgets" old posts, so cooldown decreases over time when idle.
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

    # Cooldown formula coefficients:
    #   cooldown(n) = floor(A * sqrt(n-1) + B * (n-1))  minutes, for n >= 2
    #   cooldown(1) = 0  (first post is always free)
    # where n = number of posts already in the window
    "coeff_sqrt": 1.5,     # A - controls early ramp-up (gentle start)
    "coeff_linear": 0.18,  # B - controls late-stage growth (sustained pressure)

    # Hard cap: max posts allowed in one window regardless of timing
    "max_posts_in_window": 30,

    # Minimum cooldown in seconds (even at n=1, at least this much)
    "min_cooldown_seconds": 0,

    # Maximum cooldown in seconds (safety cap)
    "max_cooldown_seconds": 1800,  # 30 minutes

    # Exempt groups (e.g. moderators, admins)
    "exempt_groups": ["moderators", "admins"],
}


def get_config():
    """Merge default config with any overrides from Django settings."""
    config = DEFAULT_CONFIG.copy()
    overrides = getattr(settings, "ANTIFLOOD_CONFIG", {})
    config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# Core cooldown calculation
# ---------------------------------------------------------------------------

def compute_cooldown_minutes(n, config=None):
    """
    Compute required cooldown in minutes before the (n+1)-th post.

    Args:
        n: Number of posts already in the sliding window (1-based).
           n=1 means user has 1 post in window, asking for 2nd.
        config: Optional config dict. Uses get_config() if None.

    Returns:
        Cooldown in minutes (float). First post (n=0) always returns 0.
    """
    if config is None:
        config = get_config()

    if n <= 1:
        return 0.0

    # Shifted ramp: use (n-1) so first real cooldown at n=2 is gentle
    m = n - 1
    a = config["coeff_sqrt"]
    b = config["coeff_linear"]

    cooldown = a * math.sqrt(m) + b * m
    return max(0.0, math.floor(cooldown))


def compute_cooldown_seconds(n, config=None):
    """Same as compute_cooldown_minutes but returns seconds."""
    return compute_cooldown_minutes(n, config) * 60


# ---------------------------------------------------------------------------
# Sliding window post counting
# ---------------------------------------------------------------------------

def count_posts_in_window(user, config=None):
    """
    Count how many posts the user has made within the sliding window.

    Args:
        user: Django User instance.
        config: Optional config dict.

    Returns:
        Tuple of (post_count, latest_post_datetime_or_None).
    """
    if config is None:
        config = get_config()

    window_start = timezone.now() - timedelta(seconds=config["window_seconds"])

    # Import here to avoid circular imports - adjust model path as needed
    from posts.models import Post

    recent_posts = (
        Post.objects
        .filter(author=user, created_at__gte=window_start)
        .order_by("-created_at")
    )

    count = recent_posts.count()
    latest = recent_posts.values_list("created_at", flat=True).first()

    return count, latest


# ---------------------------------------------------------------------------
# Main check: can user post?
# ---------------------------------------------------------------------------

def check_can_post(user, config=None):
    """
    Check whether a user is allowed to post right now.

    Args:
        user: Django User instance.
        config: Optional config dict.

    Returns:
        dict with keys:
            - allowed (bool): True if user can post.
            - wait_seconds (int): Seconds to wait (0 if allowed).
            - posts_in_window (int): Current post count in window.
            - cooldown_seconds (int): Required cooldown for this level.
            - message (str): Human-readable status message.
    """
    if config is None:
        config = get_config()

    # Check exemptions
    if is_user_exempt(user, config):
        return _build_result(allowed=True, wait=0, count=0, cooldown=0)

    post_count, latest_post_time = count_posts_in_window(user, config)

    # Hard cap check
    if post_count >= config["max_posts_in_window"]:
        wait = _time_until_window_slot_frees(user, config)
        return _build_result(
            allowed=False,
            wait=wait,
            count=post_count,
            cooldown=0,
            message=f"Osiągnięto limit {config['max_posts_in_window']} postów "
                    f"w oknie czasowym. Poczekaj aż starsze posty wypadną z okna."
        )

    # No posts yet - always allowed
    if post_count == 0 or latest_post_time is None:
        return _build_result(allowed=True, wait=0, count=post_count, cooldown=0)

    # Compute cooldown for current level
    cooldown_secs = compute_cooldown_seconds(post_count, config)
    cooldown_secs = _clamp_cooldown(cooldown_secs, config)

    elapsed = (timezone.now() - latest_post_time).total_seconds()
    remaining = cooldown_secs - elapsed

    if remaining <= 0:
        return _build_result(
            allowed=True, wait=0, count=post_count, cooldown=cooldown_secs
        )

    wait = int(math.ceil(remaining))
    return _build_result(
        allowed=False,
        wait=wait,
        count=post_count,
        cooldown=cooldown_secs,
        message=_format_wait_message(wait)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_user_exempt(user, config):
    """Check if user belongs to any exempt group."""
    exempt = config.get("exempt_groups", [])
    if not exempt:
        return False
    return user.groups.filter(name__in=exempt).exists()


def _clamp_cooldown(cooldown_secs, config):
    """Apply min/max bounds to cooldown value."""
    lo = config.get("min_cooldown_seconds", 0)
    hi = config.get("max_cooldown_seconds", 1800)
    return max(lo, min(hi, cooldown_secs))


def _time_until_window_slot_frees(user, config):
    """
    When hard cap is hit, calculate seconds until the oldest post
    in the window expires (falls outside the sliding window).
    """
    window_start = timezone.now() - timedelta(seconds=config["window_seconds"])

    from posts.models import Post

    oldest_in_window = (
        Post.objects
        .filter(author=user, created_at__gte=window_start)
        .order_by("created_at")
        .values_list("created_at", flat=True)
        .first()
    )

    if oldest_in_window is None:
        return 0

    expires_at = oldest_in_window + timedelta(seconds=config["window_seconds"])
    remaining = (expires_at - timezone.now()).total_seconds()
    return max(0, int(math.ceil(remaining)))


def _build_result(allowed, wait, count, cooldown, message=None):
    """Construct a standardized result dict."""
    if message is None:
        message = "OK" if allowed else _format_wait_message(wait)

    return {
        "allowed": allowed,
        "wait_seconds": wait,
        "posts_in_window": count,
        "cooldown_seconds": int(cooldown),
        "message": message,
    }


def _format_wait_message(wait_seconds):
    """Format wait time into a human-readable Polish message."""
    if wait_seconds <= 0:
        return "Możesz pisać."

    if wait_seconds < 60:
        return f"Poczekaj {wait_seconds} sek. przed kolejnym postem."

    minutes = wait_seconds // 60
    secs = wait_seconds % 60

    if minutes < 60:
        if secs > 0:
            return f"Poczekaj {minutes} min {secs} sek. przed kolejnym postem."
        return f"Poczekaj {minutes} min przed kolejnym postem."

    hours = minutes // 60
    mins = minutes % 60
    if mins > 0:
        return f"Poczekaj {hours}h {mins} min przed kolejnym postem."
    return f"Poczekaj {hours}h przed kolejnym postem."


# ---------------------------------------------------------------------------
# Debug / analysis utility
# ---------------------------------------------------------------------------

def print_cooldown_table(config=None):
    """
    Print the full cooldown table for analysis/debugging.
    Shows cumulative time to reach N posts at minimum intervals.
    """
    if config is None:
        config = get_config()

    max_n = config["max_posts_in_window"]
    cumulative_min = 0.0

    print(f"{'Post #':<8} {'Cooldown':<12} {'Cumulative':<15}")
    print("-" * 35)

    for n in range(1, max_n + 1):
        cd = compute_cooldown_minutes(n, config)
        cumulative_min += cd

        hours = int(cumulative_min // 60)
        mins = int(cumulative_min % 60)

        if hours > 0:
            cum_str = f"{hours}h {mins:02d} min"
        else:
            cum_str = f"{mins} min"

        print(f"{n + 1:<8} {int(cd):<12} {cum_str:<15}")
