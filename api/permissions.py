from rest_framework.permissions import BasePermission
from board.models import User


class IsModerator(BasePermission):
    """Moderator or higher (role >= ROLE_MODERATOR, or is_root)."""

    message = "Wymagana rola moderatora."

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return u.is_root or u.role >= User.ROLE_MODERATOR


class IsAdmin(BasePermission):
    """Admin or higher (role >= ROLE_ADMIN, or is_root)."""

    message = "Wymagana rola administratora."

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return u.is_root or u.role >= User.ROLE_ADMIN
