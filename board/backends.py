"""
Custom authentication backend.

Transparently handles two login paths:
  - Browser (JS): password already prehashed, POST contains password_is_prehashed=1
  - Admin panel / CLI: plaintext password → we prehash here in Python
"""

from django.contrib.auth.backends import ModelBackend

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

        return super().authenticate(
            request, username=username, password=password, **kwargs
        )
