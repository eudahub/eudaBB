"""Authentication endpoints.

Flow (Android):
    register-init → [Argon2id locally] → register  → JWT pair
    login-init    → [Argon2id locally] → login     → JWT pair
    refresh       → new JWT pair
    logout        → (client discards tokens; server side is stateless)
    reset-request → email with 6-digit code
    reset-confirm → verifies code, sets new password
"""

import base64
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken

from api import response as R
from api.serializers import MeSerializer, _role_string
from board.auth_utils import SITE_SALT_SUFFIX
from board.username_utils import normalize

User = get_user_model()


def _make_tokens(user):
    """Generate JWT pair with extra claims (username, role)."""
    refresh = RefreshToken.for_user(user)
    refresh["username"] = user.username
    refresh["role"] = _role_string(user)
    access = refresh.access_token
    return {
        "token": str(access),
        "refresh_token": str(refresh),
        "expires_in": int(access.lifetime.total_seconds()),
        "user": MeSerializer(user).data,
    }


def _compute_salt_b64(username: str) -> str:
    """Return base64-encoded salt string for the given username.

    salt = normalize(username) + SITE_SALT_SUFFIX  (same as auth_utils.prehash_password)
    """
    salt_bytes = (normalize(username) + SITE_SALT_SUFFIX).encode()
    return base64.b64encode(salt_bytes).decode()


# ---------------------------------------------------------------------------
# GET /api/v1/auth/argon2-params
# ---------------------------------------------------------------------------

class Argon2ParamsView(APIView):
    """Return Argon2id parameters the client must use for prehashing.

    Called once on app start to ensure client has up-to-date parameters.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from board.auth_utils import PREHASH_MEMORY, PREHASH_TIME, PREHASH_PARALLEL, PREHASH_HASHLEN
        return R.ok({
            "variant": "argon2id",
            "memory_kib": PREHASH_MEMORY,
            "iterations": PREHASH_TIME,
            "parallelism": PREHASH_PARALLEL,
            "hash_len": PREHASH_HASHLEN,
            "salt_suffix": SITE_SALT_SUFFIX,
        })


# ---------------------------------------------------------------------------
# POST /api/v1/auth/register-init
# ---------------------------------------------------------------------------

class RegisterInitView(APIView):
    """Return salt for a new username (no DB write).

    Validates that the username is not already taken before Android does the
    expensive Argon2id computation.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        if not username:
            return R.error("MISSING_FIELD", "Wymagane pole: username.")

        norm = normalize(username)
        if User.objects.filter(username_normalized=norm).exists():
            return R.error("USERNAME_TAKEN", "Ta nazwa użytkownika jest już zajęta.", 409)

        return R.ok({"salt": _compute_salt_b64(username)})


# ---------------------------------------------------------------------------
# POST /api/v1/auth/register
# ---------------------------------------------------------------------------

class RegisterView(APIView):
    """Create a new user account and return JWT pair.

    Simplified flow — no email verification code (Android registration).
    The email is stored as-is for future password recovery.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        password_hash = (request.data.get("password_hash") or "").strip()
        email = (request.data.get("email") or "").strip().lower()

        # --- basic field validation ---
        if not username:
            return R.error("MISSING_FIELD", "Wymagane pole: username.")
        if not password_hash:
            return R.error("MISSING_FIELD", "Wymagane pole: password_hash.")
        if not email:
            return R.error("MISSING_FIELD", "Wymagane pole: email.")

        # --- username validation ---
        from board.forms import _RESERVED_USERNAME_NORMS, _RESERVED_USERNAME_PATTERN
        norm = normalize(username)
        if norm in _RESERVED_USERNAME_NORMS or _RESERVED_USERNAME_PATTERN.match(norm):
            return R.error("USERNAME_RESERVED", "Ta nazwa użytkownika jest zarezerwowana.")

        existing = User.objects.filter(username_normalized=norm).first()
        if existing:
            if existing.is_ghost() and existing.username == username:
                return R.error(
                    "USERNAME_GHOST",
                    "To konto jest archiwalne. Skontaktuj się z administratorem, aby je przejąć.",
                    409,
                )
            return R.error("USERNAME_TAKEN", "Ta nazwa użytkownika jest już zajęta.", 409)

        # --- email validation ---
        from board.forms import _check_email_domain
        email_err = _check_email_domain(email)
        if email_err:
            return R.error("EMAIL_DOMAIN_BLOCKED", email_err)

        from board.models import SpamEmail
        if SpamEmail.objects.filter(email=email).exists():
            return R.error("EMAIL_BLOCKED", "Ten adres email nie może być użyty do rejestracji.")

        if User.objects.filter(email=email).exclude(email="").exists():
            return R.error("EMAIL_TAKEN", "Ten adres email jest już zarejestrowany.", 409)

        # --- create user ---
        user = User(username=username, email=email)
        user.set_password(password_hash)  # password_hash is the Argon2id prehash
        user.save()

        return R.created(_make_tokens(user))


# ---------------------------------------------------------------------------
# POST /api/v1/auth/login-init
# ---------------------------------------------------------------------------

class LoginInitView(APIView):
    """Return salt for existing user before Android computes Argon2id."""
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        if not username:
            return R.error("MISSING_FIELD", "Wymagane pole: username.")

        if not User.objects.filter(username_normalized=normalize(username)).exists():
            return R.error("USER_NOT_FOUND", "Nie znaleziono użytkownika.", 404)

        return R.ok({"salt": _compute_salt_b64(username)})


# ---------------------------------------------------------------------------
# POST /api/v1/auth/login
# ---------------------------------------------------------------------------

class LoginView(APIView):
    """Verify Argon2id prehash and return JWT pair."""
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        password_hash = (request.data.get("password_hash") or "").strip()

        if not username or not password_hash:
            return R.error("MISSING_FIELD", "Wymagane pola: username, password_hash.")

        try:
            user = User.objects.get(username_normalized=normalize(username))
        except User.DoesNotExist:
            return R.error("INVALID_CREDENTIALS", "Nieprawidłowy nick lub hasło.", 401)

        # Lift expired temporary ban before checking is_active
        if not user.is_active and user.banned_until:
            if user.banned_until <= timezone.now():
                user.is_active = True
                user.banned_until = None
                user.save(update_fields=["is_active", "banned_until"])

        if not user.is_active:
            if user.banned_until:
                until = user.banned_until.strftime("%Y-%m-%dT%H:%M:%SZ")
                return R.error("BANNED", f"Konto zablokowane do {until}.", 403)
            return R.error("BANNED", "Konto zostało permanentnie zablokowane.", 403)

        if not user.has_usable_password():
            return R.error(
                "NO_PASSWORD",
                "To konto jest archiwalne. Skontaktuj się z administratorem.",
                403,
            )

        if not user.check_password(password_hash):
            return R.error("INVALID_CREDENTIALS", "Nieprawidłowy nick lub hasło.", 401)

        return R.ok(_make_tokens(user))


# ---------------------------------------------------------------------------
# POST /api/v1/auth/refresh
# ---------------------------------------------------------------------------

class TokenRefreshView(APIView):
    """Rotate refresh token and return new JWT pair."""
    permission_classes = [AllowAny]

    def post(self, request):
        token_str = (request.data.get("refresh_token") or "").strip()
        if not token_str:
            return R.error("MISSING_FIELD", "Wymagane pole: refresh_token.")
        try:
            refresh = RefreshToken(token_str)
            user = User.objects.get(pk=refresh["user_id"])
        except Exception:
            return R.error("INVALID_TOKEN", "Nieprawidłowy lub wygasły token.", 401)

        return R.ok(_make_tokens(user))


# ---------------------------------------------------------------------------
# POST /api/v1/auth/logout
# ---------------------------------------------------------------------------

class LogoutView(APIView):
    """Client-side logout. No server-side token blacklist (stateless JWT).

    The client should discard both tokens from EncryptedSharedPreferences.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return R.ok({"message": "Wylogowano."})


# ---------------------------------------------------------------------------
# POST /api/v1/auth/reset-request
# ---------------------------------------------------------------------------

class ResetRequestView(APIView):
    """Request password reset email. Uses existing PasswordResetCode flow."""
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        if not username:
            return R.error("MISSING_FIELD", "Wymagane pole: username.")

        try:
            user = User.objects.get(username_normalized=normalize(username))
        except User.DoesNotExist:
            # Don't reveal whether user exists
            return R.ok({"message": "Jeśli konto istnieje, kod został wysłany na adres email."})

        if user.is_root or not user.email:
            return R.ok({"message": "Jeśli konto istnieje, kod został wysłany na adres email."})

        # Reuse rate-limit check, code generation and email send from web views
        from board.views import (
            _can_send_reset_code,
            _generate_reset_code,
            _send_reset_code_email,
        )
        from board.models import PasswordResetCode
        from datetime import timedelta

        allowed, _ = _can_send_reset_code(user)
        if not allowed:
            return R.error(
                "RATE_LIMITED",
                f"Wysłano już {PasswordResetCode.MAX_PER_HOUR} kody w ciągu ostatniej godziny. Spróbuj później.",
                429,
            )

        code = _generate_reset_code()
        expires = timezone.now() + timedelta(hours=PasswordResetCode.CODE_EXPIRY_HOURS)
        PasswordResetCode.objects.create(user=user, code=code, expires_at=expires)
        _send_reset_code_email(user, code, user.email)

        return R.ok({"message": "Jeśli konto istnieje, kod został wysłany na adres email."})


# ---------------------------------------------------------------------------
# POST /api/v1/auth/reset-confirm
# ---------------------------------------------------------------------------

class ResetConfirmView(APIView):
    """Verify reset code and set new password (prehashed)."""
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        code = (request.data.get("code") or "").strip()
        password_hash = (request.data.get("password_hash") or "").strip()

        if not username or not code or not password_hash:
            return R.error("MISSING_FIELD", "Wymagane pola: username, code, password_hash.")

        try:
            user = User.objects.get(username_normalized=normalize(username))
        except User.DoesNotExist:
            return R.error("INVALID_CODE", "Nieprawidłowy kod.", 400)

        from board.models import PasswordResetCode
        valid_code = (
            PasswordResetCode.objects
            .filter(user=user, code=code, is_used=False)
            .order_by("-created_at")
            .first()
        )
        if not valid_code or valid_code.is_expired():
            return R.error("INVALID_CODE", "Kod wygasł lub jest nieprawidłowy.", 400)

        user.set_password(password_hash)
        user.save(update_fields=["password"])
        valid_code.is_used = True
        valid_code.save(update_fields=["is_used"])

        return R.ok({"message": "Hasło zostało zmienione. Możesz się teraz zalogować."})
