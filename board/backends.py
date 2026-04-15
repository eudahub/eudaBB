"""
Custom authentication backend.

Transparently handles two login paths:
  - Browser (JS): password already prehashed, POST contains password_is_prehashed=1
  - Admin panel / CLI: plaintext password → we prehash here in Python
"""

from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from .auth_utils import prehash_password


class ClientArgon2Backend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        is_prehashed = (
            request is not None
            and request.POST.get("password_is_prehashed") == "1"
        )
        if not is_prehashed:
            password = prehash_password(password, username)

        # Auto-lift expired temporary bans before ModelBackend checks is_active.
        self._maybe_lift_ban(request, username)

        return super().authenticate(
            request, username=username, password=password, **kwargs
        )

    def _maybe_lift_ban(self, request, username):
        """If user has an expired temporary ban, activate them and flag the session."""
        from .models import User
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return
        if user.is_active:
            return
        if user.banned_until is None:
            return  # permanent ban — admin must lift manually
        if user.banned_until > timezone.now():
            return  # ban still active
        # Ban expired — lift it
        user.is_active = True
        user.banned_until = None
        user.save(update_fields=["is_active", "banned_until"])
        if request is not None:
            request.session["ban_lifted"] = True
