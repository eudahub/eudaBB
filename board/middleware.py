"""Site maintenance middleware and TOR exit node blocking middleware.

Blocks login and registration views for IPs listed in the TOR exit node table.
The IP set is cached in Django's cache backend (default 2h TTL); on a cache miss
it falls back to a DB query and re-populates the cache.
"""
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import redirect, render

from .models import BlockedIP, SiteConfig, TorExitNode

# Paths always accessible regardless of maintenance mode
_MAINTENANCE_EXEMPT = frozenset(["/przerwa/", "/admin/"])
_MAINTENANCE_EXEMPT_PREFIXES = ("/admin/",)


class MaintenanceModeMiddleware:
    """Block or restrict access based on SiteConfig.site_mode.

    normal   — no effect
    readonly — GET requests pass through; POST requests (except /admin/) get
               a 503 maintenance page
    closed   — only staff and users on MaintenanceAllowedUser may log in;
               everyone else sees the maintenance gate at /przerwa/
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info

        # Admin and gate itself are always reachable
        if path.startswith("/admin/") or path == "/przerwa/":
            return self.get_response(request)

        cfg = SiteConfig.get()
        mode = cfg.site_mode

        if mode == SiteConfig.MODE_READONLY:
            if request.method == "POST":
                return render(
                    request,
                    "board/maintenance_gate.html",
                    {
                        "message": cfg.maintenance_message or "Forum jest teraz w trybie tylko do odczytu.",
                        "readonly": True,
                    },
                    status=503,
                )
            return self.get_response(request)

        if mode == SiteConfig.MODE_CLOSED:
            if request.session.get("maintenance_access"):
                return self.get_response(request)
            # No service session → gate
            return redirect_to_gate(request, cfg)

        return self.get_response(request)


def redirect_to_gate(request, cfg):
    from django.shortcuts import redirect
    return redirect("/przerwa/")

# Paths restricted for blocked IPs
_BLOCKED_PATHS = frozenset([
    "/login/",
    "/register/",
    "/activate-ghost/",
])
_BLOCKED_PREFIXES = ("/activate/",)

_TOR_CACHE_KEY = "tor_exit_ips"
_PROXY_CACHE_KEY = "admin_blocked_ips"
CACHE_TIMEOUT = 7200  # 2h


def _get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _load_tor_ips():
    ips = cache.get(_TOR_CACHE_KEY)
    if ips is None:
        ips = frozenset(TorExitNode.objects.values_list("ip_address", flat=True))
        cache.set(_TOR_CACHE_KEY, ips, CACHE_TIMEOUT)
    return ips


def _load_blocked_ips():
    ips = cache.get(_PROXY_CACHE_KEY)
    if ips is None:
        ips = frozenset(BlockedIP.objects.values_list("ip_address", flat=True))
        cache.set(_PROXY_CACHE_KEY, ips, CACHE_TIMEOUT)
    return ips


def invalidate_blocked_ips_cache():
    """Call after adding/removing BlockedIP rows so middleware picks up changes immediately."""
    cache.delete(_PROXY_CACHE_KEY)


_SESSION_TRACK_TTL = 60  # seconds between DB writes per session


class SessionTrackingMiddleware:
    """Records ip_address + last_seen for each authenticated session.

    Uses a short-lived cache flag to avoid a DB write on every request —
    at most one write per _SESSION_TRACK_TTL seconds per session.
    Stale rows (older than Django SESSION_COOKIE_AGE) are lazily pruned.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated and hasattr(request, "session") and request.session.session_key:
            self._track(request)
        return response

    def _track(self, request):
        from django.conf import settings as _settings
        from django.utils import timezone as _tz
        from .models import UserSession

        skey = request.session.session_key
        cache_flag = f"strack_{skey}"
        if cache.get(cache_flag):
            return  # recently written, skip

        ip = _get_client_ip(request)
        now = _tz.now()

        UserSession.objects.update_or_create(
            session_key=skey,
            defaults={"user_id": request.user.pk, "ip_address": ip, "last_seen": now},
        )

        # Lazy cleanup: remove stale sessions for this user
        max_age = getattr(_settings, "SESSION_COOKIE_AGE", 1209600)
        cutoff = now - __import__("datetime").timedelta(seconds=max_age)
        UserSession.objects.filter(user_id=request.user.pk, last_seen__lt=cutoff).delete()

        cache.set(cache_flag, True, _SESSION_TRACK_TTL)


class TorBlockMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if path in _BLOCKED_PATHS or any(path.startswith(p) for p in _BLOCKED_PREFIXES):
            ip = _get_client_ip(request)
            if ip:
                if getattr(settings, "TOR_BLOCK_ENABLED", True) and ip in _load_tor_ips():
                    return render(request, "board/tor_blocked.html",
                                  {"reason": "tor"}, status=403)
                if ip in _load_blocked_ips():
                    return render(request, "board/tor_blocked.html",
                                  {"reason": "proxy"}, status=403)
        return self.get_response(request)
