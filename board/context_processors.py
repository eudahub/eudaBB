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
    """Inject unread PM count into every template context."""
    if not request.user.is_authenticated:
        return {"pm_unread": 0}
    from board.models import PrivateMessageBox
    count = PrivateMessageBox.objects.filter(
        owner=request.user,
        box_type=PrivateMessageBox.BoxType.INBOX,
        is_read=False,
    ).count()
    return {"pm_unread": count}
