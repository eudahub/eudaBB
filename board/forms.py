from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.conf import settings
from .models import User
from .username_utils import normalize, find_similar


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ["username", "email", "password1", "password2"]

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


class NewTopicForm(forms.Form):
    title = forms.CharField(max_length=255, label="Temat")
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 10}), label="Treść (BBCode)")


class ReplyForm(forms.Form):
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 8}), label="Odpowiedź (BBCode)")
