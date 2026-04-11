"""Context manager that locks a User during rename/delete operations.

Sets user.is_processing=True for the duration, guarantees unlock in finally.
Any view that posts, quotes, or edits should check _user_is_locked().
"""

from contextlib import contextmanager
from .models import User


@contextmanager
def user_processing_lock(user: User):
    """Lock user for the duration of a rename or delete operation."""
    User.objects.filter(pk=user.pk).update(is_processing=True)
    user.is_processing = True
    try:
        yield
    finally:
        User.objects.filter(pk=user.pk).update(is_processing=False)


def user_is_locked(user: User | None) -> bool:
    """Return True if user is currently being processed (rename/delete in progress)."""
    if user is None:
        return False
    # Re-read from DB to get current state
    return User.objects.filter(pk=user.pk, is_processing=True).exists()
