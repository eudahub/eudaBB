from django import forms
from django.contrib.auth.forms import UserCreationForm
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
                if User.objects.get(username=existing).is_ghost:
                    raise forms.ValidationError(
                        f"Nazwa '{existing}' pochodzi z archiwum. "
                        "Skontaktuj się z administratorem, aby aktywować to konto."
                    )
                raise forms.ValidationError("Ta nazwa użytkownika jest już zajęta.")

        # Similarity check
        similar = find_similar(proposed, list(all_users), max_dist=3)
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
