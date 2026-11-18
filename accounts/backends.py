from django.contrib.auth.backends import ModelBackend


class PasswordOptionalBackend(ModelBackend):
    """Autorise la connexion avec un mot de passe vide, sauf pour les comptes d'administration."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is not None and not password and (user.is_staff or user.is_superuser):
            return None
        return user
