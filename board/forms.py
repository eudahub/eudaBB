from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.conf import settings
from .models import User
from .username_utils import normalize, find_similar
from .email_utils import hash_email, mask_email, mask_email_variants
from .auth_utils import prehash_password


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True, help_text="Nie jest przechowywany — tylko zaszyfrowany skrót.")
    password_is_prehashed = forms.CharField(required=False, widget=forms.HiddenInput, initial="0")

    class Meta:
        model = User
        fields = ["username", "password1", "password2"]

    def save(self, commit=True, mask_variant=None):
        user = super().save(commit=False)
        raw_email = self.cleaned_data.get("email", "")
        user.email = ""
        user.email_hash = hash_email(raw_email)
        variants = mask_email_variants(raw_email)
        if mask_variant and mask_variant in variants:
            user.email_mask = mask_variant
        else:
            user.email_mask = mask_email(raw_email)

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

        all_users = User.objects.values_list("username", flat=True)

        # Exact normalized match
        for existing in all_users:
            if normalize(existing) == norm_proposed:
                user = User.objects.get(username=existing)
                if user.is_ghost:
                    if proposed == existing:
                        # Exact string match — allow registration, flag as pending activation
                        self._ghost_username = existing
                        return proposed
                    else:
                        # Same normalized form but different string — blocked
                        raise forms.ValidationError(
                            f"Nazwa zarezerwowana przez konto archiwalne '{existing}'. "
                            "Skontaktuj się z administratorem."
                        )
                raise forms.ValidationError("Ta nazwa użytkownika jest już zajęta.")

        # Similarity check (0 = disabled, default 1)
        max_dist = getattr(settings, "USERNAME_SIMILARITY_MAX_DIST", 1)
        similar = find_similar(proposed, list(all_users), max_dist=max_dist) if max_dist > 0 else []
        if similar:
            raise forms.ValidationError(
                f"Nazwa zbyt podobna do istniejącej: {', '.join(similar)}. "
                "Wybierz inną lub skontaktuj się z administratorem."
            )

        return proposed


def _validate_post_content(content: str, original_size: int = 0) -> str:
    """Validate post content size.

    original_size — byte length of the existing post being edited (0 for new posts).
    Rule: new_size <= max(original_size, SOFT_MAX).
    Hard limit is always enforced.
    """
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
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}), label="Odpowiedź (BBCode)")

    def __init__(self, *args, original_size: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_size = original_size

    def clean_content(self):
        return _validate_post_content(self.cleaned_data["content"], self._original_size)
