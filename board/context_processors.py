from django.conf import settings


def test_mode(request):
    from board.models import SiteConfig
    cfg = SiteConfig.get()
    return {
        "TEST_MODE": getattr(settings, "TEST_MODE", False),
        "SITE_NOTICE": getattr(settings, "SITE_NOTICE", ""),
        "site_config": cfg,
    }


def pm_unread_count(request):
    """Inject unread PM count and notification count into every template context."""
    if not request.user.is_authenticated:
        return {"pm_unread": 0, "notif_count": 0}
    from board.models import Notification, PrivateMessageBox, Post
    pm_count = PrivateMessageBox.objects.filter(
        owner=request.user,
        box_type=PrivateMessageBox.BoxType.INBOX,
        is_read=False,
    ).count()
    notif_qs = Notification.objects.filter(recipient=request.user, is_read=False)
    # PENDING_QUEUE counts only when queue is actually non-empty
    has_pending = Post.objects.filter(is_pending=True).exists()
    if not has_pending:
        notif_qs = notif_qs.exclude(notif_type=Notification.Type.PENDING_QUEUE)
    notif_count = notif_qs.count()
    return {"pm_unread": pm_count, "notif_count": notif_count}


def user_session_info(request):
    """Inject distinct IP lists for the forum user and the maintenance gate user."""
    from django.core.cache import cache
    from board.models import UserSession

    def _ips_for_user_id(uid):
        cache_key = f"session_ips_{uid}"
        ips = cache.get(cache_key)
        if ips is None:
            ips = list(
                UserSession.objects
                .filter(user_id=uid)
                .values_list("ip_address", flat=True)
                .distinct()
            )
            cache.set(cache_key, ips, 60)
        return ips

    forum_ips = []
    if request.user.is_authenticated:
        forum_ips = _ips_for_user_id(request.user.pk)

    maint_ips = []
    maint_username = request.session.get("maintenance_user")
    if maint_username:
        from board.models import User as ForumUser
        maint_user = ForumUser.objects.filter(username=maint_username).only("pk").first()
        if maint_user:
            maint_ips = _ips_for_user_id(maint_user.pk)

    return {
        "user_ips": forum_ips,
        "user_ip_count": len(forum_ips),
        "maint_ips": maint_ips,
        "maint_ip_count": len(maint_ips),
    }
