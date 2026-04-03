"""TOR exit node blocking middleware.

Blocks login and registration views for IPs listed in the TOR exit node table.
The IP set is cached in Django's cache backend (default 2h TTL); on a cache miss
it falls back to a DB query and re-populates the cache.
"""
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render

from .models import BlockedIP, TorExitNode

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
