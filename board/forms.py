from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.conf import settings
from .models import User
from .username_utils import normalize
from .email_utils import mask_email, mask_email_variants
from .auth_utils import prehash_password


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    password_is_prehashed = forms.CharField(required=False, widget=forms.HiddenInput, initial="0")

    class Meta:
        model = User
        fields = ["username", "password1", "password2"]

    def save(self, commit=True, mask_variant=None):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get("email", "").strip().lower()

        # If password arrived as plaintext (no JS), prehash now for consistency
        if self.cleaned_data.get("password_is_prehashed") != "1":
            user.set_password(prehash_password(
                self.cleaned_data["password1"], user.username
            ))

        if commit:
            user.save()
        return user

    def clean_username(self):
        proposed = self.cleaned_data["username"]
        norm_proposed = normalize(proposed)

        # O(1) lookup via indexed username_normalized column
        conflict = User.objects.filter(username_normalized=norm_proposed).first()
        if conflict:
            if conflict.is_ghost and conflict.username == proposed:
                # Exact match to ghost account — allow, trigger activation flow
                self._ghost_username = conflict.username
                return proposed
            if conflict.is_ghost:
                raise forms.ValidationError(
                    f"Nazwa zarezerwowana przez konto archiwalne '{conflict.username}'. "
                    "Skontaktuj się z administratorem."
                )
            raise forms.ValidationError("Ta nazwa użytkownika jest już zajęta.")

        return proposed


def _validate_post_content(content: str, original_size: int = 0) -> str:
    """Repair BBCode, validate markup, then check size limits.

    Returns repaired content if valid; raises ValidationError otherwise.
    original_size — byte length of the existing post being edited (0 for new posts).
    """
    from .bbcode_lint import repair_and_validate

    repaired, changes, errors = repair_and_validate(content)

    if errors:
        error_lines = "\n".join(f"• {e}" for e in errors)
        hint = ""
        if changes:
            hint = "\n\nAutomatycznie naprawiono:\n" + "\n".join(f"✓ {c}" for c in changes)
        raise forms.ValidationError(
            f"Błędy w kodzie BBCode:\n{error_lines}{hint}"
        )

    content = repaired

    hard = getattr(settings, "POST_CONTENT_HARD_MAX_BYTES", 64 * 1024)
    soft = getattr(settings, "POST_CONTENT_SOFT_MAX_BYTES", 20_000)

    new_size = len(content.encode("utf-8"))

    if new_size > hard:
        raise forms.ValidationError(
            f"Treść za długa: {new_size} B (twardy limit: {hard // 1024} kB)."
        )

    allowed = max(original_size, soft)
    if new_size > allowed:
        if original_size > soft:
            raise forms.ValidationError(
                f"Treść za długa: {new_size} B. Post miał {original_size} B — "
                f"przy edycji można tylko zmniejszyć (max {original_size} B)."
            )
        else:
            raise forms.ValidationError(
                f"Treść za długa: {new_size} B (limit: {soft} B = {soft // 1000} kB)."
            )

    return content


class NewTopicForm(forms.Form):
    title = forms.CharField(max_length=255, label="Temat")
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 10}), label="Treść (BBCode)")

    def clean_content(self):
        return _validate_post_content(self.cleaned_data["content"])


class ReplyForm(forms.Form):
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}), label="Treść")

    def __init__(self, *args, original_size: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_size = original_size

    def clean_content(self):
        return _validate_post_content(self.cleaned_data["content"], self._original_size)
