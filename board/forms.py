from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.conf import settings
from .models import User
from .username_utils import normalize
from .email_utils import mask_email, mask_email_variants
from .auth_utils import prehash_password
from .polls import validate_poll_option_count


def _check_email_domain(email: str) -> str | None:
    """Validate email domain. Returns error message or None if ok."""
    try:
        import tldextract
        from .models import SpamDomain
        host = email.split("@", 1)[-1].lower().strip()
        ext = tldextract.extract(host)
        if not ext.suffix:
            return "Adres email ma nieprawidłowe rozszerzenie domeny."
        name = ext.domain
        if name.startswith("mail2"):
            return "Adres email pochodzi z domeny o podejrzanie długiej nazwie."
        if ext.suffix == "info" or ext.suffix.endswith(".info"):
            return "Adres email pochodzi z domeny o podejrzanie długiej nazwie."
        if len(name) > 29:
            return "Adres email pochodzi z domeny o podejrzanie długiej nazwie."
        if ("mail" in name or "box" in name) and len(name) > 11:
            return "Adres email pochodzi z domeny o podejrzanie długiej nazwie."
        base = f"{name}.{ext.suffix}"
        if SpamDomain.objects.filter(domain=base, spam=1).exists():
            return "Adres email wygląda na tymczasowy lub spambox. Użyj stałego adresu email."
    except Exception:
        pass
    return None

TOPIC_TITLE_MAX_LENGTH = 70

# Normalized usernames permanently reserved by the system
_RESERVED_USERNAME_NORMS = frozenset({
    "usuniety",   # display label for deleted accounts in quotes
})


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

        if norm_proposed in _RESERVED_USERNAME_NORMS:
            raise forms.ValidationError("Ta nazwa użytkownika jest zarezerwowana przez system.")

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


class RegisterStartForm(forms.Form):
    username = forms.CharField(max_length=150, label="Nick")
    email = forms.EmailField(label="Email")

    def clean_username(self):
        proposed = self.cleaned_data["username"]
        norm_proposed = normalize(proposed)
        if norm_proposed in _RESERVED_USERNAME_NORMS:
            raise forms.ValidationError("Ta nazwa użytkownika jest zarezerwowana przez system.")
        conflict = User.objects.filter(username_normalized=norm_proposed).first()
        if not conflict:
            return proposed
        if conflict.is_ghost:
            raise forms.ValidationError(
                f"To konto już istnieje w archiwum jako '{conflict.username}'. "
                "Użyj odzyskiwania konta zamiast nowej rejestracji."
            )
        raise forms.ValidationError("Ta nazwa użytkownika jest już zajęta.")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        err = _check_email_domain(email)
        if err:
            raise forms.ValidationError(err)
        conflict = User.objects.filter(email=email).first()
        if not conflict:
            return email
        if conflict.is_ghost:
            raise forms.ValidationError(
                "Ten email jest już przypisany do konta archiwalnego. "
                "Użyj odzyskiwania konta zamiast nowej rejestracji."
            )
        raise forms.ValidationError("Ten email jest już przypisany do istniejącego konta.")


class RegisterFinishForm(forms.Form):
    code = forms.CharField(max_length=6, min_length=6, label="Kod", strip=True)
    password1 = forms.CharField(widget=forms.PasswordInput, label="Hasło")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Powtórz hasło")
    password_is_prehashed = forms.CharField(
        required=False, widget=forms.HiddenInput, initial="0"
    )

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        if not code.isdigit():
            raise forms.ValidationError("Kod musi składać się z 6 cyfr.")
        return code

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Hasła nie są identyczne.")
        return cleaned


def parse_poll_options_text(raw_text: str) -> tuple[list[dict], list[str]]:
    """Parse poll options textarea.

    Returns (options, errors) where options is list of {"text": str, "category": str}.
    Format: lines starting with '-' are options, '##' lines are category headers,
    blank lines reset current category, other non-empty lines are errors.
    """
    current_category = ""
    options = []
    bad_lines = []
    empty_category_lines = []
    declared_categories = []
    categories_with_options = set()

    for ln in raw_text.splitlines():
        stripped = ln.strip()
        if not stripped:
            current_category = ""
            continue
        if stripped.startswith("-"):
            text = stripped[1:].strip()
            if text:
                options.append({"text": text, "category": current_category})
                if current_category:
                    categories_with_options.add(current_category)
        elif stripped.startswith("##"):
            cat = stripped[2:].strip()
            if not cat:
                empty_category_lines.append(stripped)
            else:
                current_category = cat
                if cat not in declared_categories:
                    declared_categories.append(cat)
        else:
            bad_lines.append(stripped)

    errors = []
    if bad_lines:
        examples = ", ".join(f'„{ln}"' for ln in bad_lines[:3])
        errors.append(f"Nieznane linie (muszą zaczynać się od - lub ##): {examples}.")
    if empty_category_lines:
        errors.append("Nazwa kategorii (##) nie może być pusta.")
    empty_categories = [c for c in declared_categories if c not in categories_with_options]
    if empty_categories:
        examples = ", ".join(f'„{c}"' for c in empty_categories[:3])
        errors.append(f"Kategorie bez opcji: {examples}.")

    seen = set()
    duplicates = []
    for opt in options:
        t = opt["text"].lower()
        if t in seen:
            duplicates.append(opt["text"])
        seen.add(t)
    if duplicates:
        examples = ", ".join(f'„{t}"' for t in duplicates[:3])
        errors.append(f"Opcje muszą być unikalne. Duplikaty: {examples}.")

    return options, errors


def poll_options_to_text(options) -> str:
    """Reconstruct textarea text from a queryset/list of PollOption objects."""
    lines = []
    current_cat = object()  # sentinel
    for opt in options:
        if opt.category != current_cat:
            if lines:
                lines.append("")  # blank line before new category section
            if opt.category:
                lines.append(f"## {opt.category}")
            current_cat = opt.category
        lines.append(f"- {opt.option_text}")
    return "\n".join(lines)


def validate_post_content(content: str, original_size: int = 0) -> tuple[str, list[str], list[str]]:
    """Repair and validate post content.

    Returns (repaired_content, auto_changes, errors).
    original_size — character length of the existing post being edited (0 for new posts).
    """
    from .bbcode_lint import repair_and_validate
    from .quote_validation import validate_enriched_quotes

    repaired, changes, errors = repair_and_validate(content)

    if errors:
        return repaired, changes, [str(e) for e in errors]

    content = repaired

    hard = getattr(settings, "POST_CONTENT_HARD_MAX_CHARS", 65_535)
    soft = getattr(settings, "POST_CONTENT_SOFT_MAX_CHARS", 20_000)

    new_size = len(content)

    if new_size > hard:
        return content, changes, [
            f"Treść za długa: {new_size} znaków (twardy limit: {hard})."
        ]

    allowed = max(original_size, soft)
    if new_size > allowed:
        if original_size > soft:
            return content, changes, [
                f"Treść za długa: {new_size} znaków. Post miał {original_size} znaków — "
                f"przy edycji można tylko zmniejszyć (max {original_size})."
            ]
        else:
            return content, changes, [
                f"Treść za długa: {new_size} znaków (limit: {soft})."
            ]

    quote_errors = validate_enriched_quotes(content)
    if quote_errors:
        return content, changes, quote_errors

    return content, changes, []


def _validate_post_content(content: str, original_size: int = 0) -> str:
    """Return cleaned content or raise ValidationError for form usage."""
    repaired, changes, errors = validate_post_content(content, original_size)

    if errors:
        error_lines = "\n".join(f"• {e}" for e in errors)
        hint = ""
        if changes:
            hint = "\n\nAutomatycznie naprawiono:\n" + "\n".join(f"✓ {c}" for c in changes)
        raise forms.ValidationError(
            f"Błędy w kodzie BBCode:\n{error_lines}{hint}"
        )

    return repaired


def validate_pm_content(content: str, original_size: int = 0) -> tuple[str, list[str], list[str]]:
    from .bbcode_lint import repair_and_validate

    repaired, changes, errors = repair_and_validate(content)
    if errors:
        return repaired, changes, [str(e) for e in errors]

    hard = getattr(settings, "PM_CONTENT_HARD_MAX_CHARS", 65_535)
    soft = getattr(settings, "PM_CONTENT_SOFT_MAX_CHARS", 20_000)
    new_size = len(repaired)

    if new_size > hard:
        return repaired, changes, [
            f"Treść za długa: {new_size} znaków (twardy limit: {hard})."
        ]

    allowed = max(original_size, soft)
    if new_size > allowed:
        if original_size > soft:
            return repaired, changes, [
                f"Treść za długa: {new_size} znaków. Wiadomość miała {original_size} znaków — "
                f"przy edycji można tylko zmniejszyć (max {original_size})."
            ]
        return repaired, changes, [
            f"Treść za długa: {new_size} znaków (limit: {soft})."
        ]

    return repaired, changes, []


class NewTopicForm(forms.Form):
    title = forms.CharField(max_length=TOPIC_TITLE_MAX_LENGTH, label="Temat")
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 10}), label="Treść (BBCode)")
    poll_enabled = forms.BooleanField(required=False)
    poll_question = forms.CharField(required=False)
    poll_duration_days = forms.IntegerField(required=False, min_value=1)
    poll_allow_vote_change = forms.BooleanField(required=False)
    poll_allow_multiple_choice = forms.BooleanField(required=False)

    def __init__(self, *args, is_admin: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._is_admin = is_admin

    def clean_content(self):
        return _validate_post_content(self.cleaned_data["content"])

    def clean(self):
        cleaned = super().clean()
        # Poll is requested ONLY when the hidden poll_enabled input is literally "1".
        # We read raw POST data because BooleanField uses CheckboxInput widget,
        # which returns bool("0") == True for non-empty string "0" — wrong here.
        if self.data.get("poll_enabled") != "1":
            cleaned["poll_data"] = None
            return cleaned

        raw_text = self.data.get("poll_options_text", "")
        poll_options, option_errors = parse_poll_options_text(raw_text)

        poll_question = (cleaned.get("poll_question") or "").strip()
        duration = cleaned.get("poll_duration_days")
        allow_vote_change = bool(cleaned.get("poll_allow_vote_change"))
        allow_multiple_choice = bool(cleaned.get("poll_allow_multiple_choice"))

        errors = []
        if not poll_question:
            errors.append("Podaj pytanie ankiety.")
        errors.extend(option_errors)
        if not option_errors and len(poll_options) < 2:
            errors.append("Ankieta musi mieć co najmniej 2 niepuste opcje (linie zaczynające się od -).")
        _, option_errors = validate_poll_option_count(len(poll_options))
        errors.extend(option_errors)
        if not duration and not self._is_admin:
            errors.append("Podaj czas trwania ankiety w dniach.")

        if errors:
            raise forms.ValidationError(errors)

        cleaned["poll_data"] = {
            "question": poll_question,
            "duration_days": int(duration) if duration else None,
            "allow_vote_change": allow_vote_change,
            "allow_multiple_choice": allow_multiple_choice,
            "options": poll_options,  # list of {"text": str, "category": str}
        }
        return cleaned


class ReplyForm(forms.Form):
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}), label="Treść")

    def __init__(self, *args, original_size: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_size = original_size

    def clean_content(self):
        return _validate_post_content(self.cleaned_data["content"], self._original_size)
