"""Helpers for time-based post moderation windows.

During configured windows, posts by new users (active_days <= 5) are held in
the moderation queue rather than published directly to the forum.
"""

from zoneinfo import ZoneInfo
from django.utils import timezone as tz

NEW_USER_ACTIVE_DAYS_THRESHOLD = 5


def is_in_moderation_window() -> bool:
    """Return True if the current UTC time falls within any active moderation window."""
    from .models import ModerationWindow

    windows = list(ModerationWindow.objects.filter(is_active=True))
    if not windows:
        return False

    now_utc = tz.now()
    for w in windows:
        try:
            local_tz = ZoneInfo(w.timezone)
        except Exception:
            continue
        now_local = now_utc.astimezone(local_tz)
        dow = now_local.weekday()  # 0=Pon, 6=Nd

        # Check day-of-week range (if configured)
        if w.day_from is not None and w.day_to is not None:
            df, dt = w.day_from, w.day_to
            if df <= dt:
                # Simple range, e.g. Mon(0)–Fri(4)
                if not (df <= dow <= dt):
                    continue
            else:
                # Wraps around weekend, e.g. Fri(4)–Mon(0)
                if not (dow >= df or dow <= dt):
                    continue

        # Check time range
        start = w.start_hour * 60 + w.start_minute
        end = w.end_hour * 60 + w.end_minute
        current = now_local.hour * 60 + now_local.minute

        if start <= end:
            # Same-day range, e.g. 06:00–13:00
            if start <= current < end:
                return True
        else:
            # Overnight range, e.g. 23:00–02:00
            if current >= start or current < end:
                return True

    return False


def should_hold_for_moderation(user) -> bool:
    """Return True if this user's post should be held pending moderation."""
    if not user or not user.is_authenticated:
        return False
    active_days = getattr(user, "active_days", 0)
    if active_days > NEW_USER_ACTIVE_DAYS_THRESHOLD:
        return False
    return is_in_moderation_window()
