from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password

from .models import User


class LoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Nom d’utilisateur ou mot de passe incorrect. "
                         "Laissez le mot de passe vide si votre compte n’en a pas.",
    }
    password = forms.CharField(
        label="Mot de passe (laissez vide si vous n’en avez pas)",
        required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password") or ""
        if username is not None:
            self.user_cache = authenticate(self.request, username=username, password=password)
            if self.user_cache is None:
                raise self.get_invalid_login_error()
            self.confirm_login_allowed(self.user_cache)
        return self.cleaned_data


class SignUpForm(forms.ModelForm):
    password1 = forms.CharField(
        label="Mot de passe (facultatif)", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Laissez ce champ et le suivant vides pour un compte sans mot de passe : "
                  "toute personne connaissant votre nom d’utilisateur pourra alors se "
                  "connecter à votre place. Avec un mot de passe, l’e-mail de récupération "
                  "est obligatoire.",
    )
    password2 = forms.CharField(
        label="Confirmation du mot de passe", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = User
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = False
        self.fields["email"].help_text = (
            "Sert uniquement à réinitialiser votre mot de passe si vous l’oubliez. "
            "Obligatoire si vous choisissez un mot de passe."
        )

    def clean_username(self):
        username = self.cleaned_data["username"]
        if len(username) < 3:
            raise forms.ValidationError("Au moins 3 caractères.")
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ce nom d’utilisateur est déjà pris.")
        return username

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1") or ""
        p2 = cleaned.get("password2") or ""
        email = (cleaned.get("email") or "").strip()
        if p1 or p2:
            if p1 != p2:
                self.add_error("password2", "Les deux mots de passe ne correspondent pas.")
            else:
                try:
                    validate_password(p1, user=User(username=cleaned.get("username") or "",
                                                    email=email))
                except forms.ValidationError as e:
                    self.add_error("password1", e)
            if not email:
                self.add_error("email", "Indiquez une adresse e-mail de récupération "
                                        "si vous choisissez un mot de passe.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data.get("password1") or "")
        if commit:
            user.save()
        return user


class ProfileForm(forms.Form):
    email = forms.EmailField(
        label="E-mail de récupération", required=False,
        help_text="Obligatoire si vous avez un mot de passe : il sert à le réinitialiser en cas d’oubli.",
    )
    current_password = forms.CharField(
        label="Mot de passe actuel", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text="Nécessaire pour changer l’e-mail, changer le mot de passe ou le supprimer.",
    )
    new_password1 = forms.CharField(
        label="Nouveau mot de passe", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Laissez vide pour ne rien changer.",
    )
    new_password2 = forms.CharField(
        label="Confirmation du nouveau mot de passe", required=False, strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    remove_password = forms.BooleanField(
        label="Supprimer mon mot de passe (compte sans mot de passe)", required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.has_password = not user.check_password("")
        if not self.has_password:
            del self.fields["current_password"]
            del self.fields["remove_password"]
            self.fields["new_password1"].help_text = (
                "Laissez vide pour rester sans mot de passe. "
                "Un mot de passe exige une adresse e-mail de récupération."
            )
        elif user.is_staff or user.is_superuser:
            del self.fields["remove_password"]

    def clean(self):
        cleaned = super().clean()
        email = User.objects.normalize_email((cleaned.get("email") or "").strip())
        p1 = cleaned.get("new_password1") or ""
        p2 = cleaned.get("new_password2") or ""
        remove = bool(cleaned.get("remove_password"))
        changing = bool(p1 or p2)
        if changing and remove:
            raise forms.ValidationError(
                "Choisissez soit un nouveau mot de passe, soit la suppression "
                "du mot de passe, pas les deux.")
        sensitive = changing or remove or email != self.user.email
        if self.has_password and sensitive and \
                not self.user.check_password(cleaned.get("current_password") or ""):
            self.add_error("current_password", "Mot de passe actuel incorrect.")
        keeps_password = changing or (self.has_password and not remove)
        if keeps_password and not email:
            self.add_error("email", "Un compte avec mot de passe doit avoir "
                                    "un e-mail de récupération.")
        if changing:
            if p1 != p2:
                self.add_error("new_password2", "Les deux mots de passe ne correspondent pas.")
            else:
                try:
                    validate_password(p1, user=self.user)
                except forms.ValidationError as e:
                    self.add_error("new_password1", e)
        return cleaned

    def save(self):
        user, cd = self.user, self.cleaned_data
        user.email = User.objects.normalize_email((cd.get("email") or "").strip())
        if cd.get("new_password1"):
            user.set_password(cd["new_password1"])
        elif cd.get("remove_password"):
            user.set_password("")
        user.save()
        return user
