"""Helpers for post report flag maintenance and notifications."""
from django.db.models import F
from django.utils import timezone


def _update_post_report_flag(post):
    """
    Recompute post.has_open_report from live DB.
    Increments / decrements topic.open_report_count accordingly.
    """
    from .models import PostReport, Topic

    has_open = PostReport.objects.filter(post=post, is_closed=False).exists()
    if post.has_open_report == has_open:
        return  # nothing changed

    post.has_open_report = has_open
    post.save(update_fields=["has_open_report"])

    delta = 1 if has_open else -1
    Topic.objects.filter(pk=post.topic_id).update(
        open_report_count=F("open_report_count") + delta
    )


def open_report(post, reporter, reason, comment):
    """Create a PostReport and update flags. Returns (report, created)."""
    from .models import PostReport
    report, created = PostReport.objects.get_or_create(
        post=post,
        reporter=reporter,
        defaults={"reason": reason, "comment": comment, "is_closed": False},
    )
    if created:
        _update_post_report_flag(post)
        # Notify moderators
        from .notifications import notify_post_reported
        notify_post_reported(post, reporter)
    return report, created


def close_report(report, moderator, resolution="resolved"):
    """Close a report and update flags. Notifies reporter.

    resolution: "resolved" (action taken) or "dismissed" (no action needed).
    """
    if report.is_closed:
        return
    report.is_closed   = True
    report.resolution  = resolution
    report.resolved_by = moderator
    report.resolved_at = timezone.now()
    report.save(update_fields=["is_closed", "resolution", "resolved_by", "resolved_at"])
    # Refresh post from DB to have current has_open_report value
    report.post.refresh_from_db(fields=["has_open_report"])
    _update_post_report_flag(report.post)
    # Notify reporter
    from .notifications import notify_report_closed_post
    notify_report_closed_post(report)
