from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.db import models
from django.db.models.functions import Lower


class UserManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, username):
        return self.get(username__iexact=username)

    def create_user(self, username, email=None, password=None, **extra):
        if not username:
            raise ValueError("Nom d’utilisateur requis")
        user = self.model(username=username, email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email=None, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(username, email, password, **extra)


class User(AbstractUser):
    username = models.CharField(
        "Nom d’utilisateur", max_length=30, unique=True,
        validators=[UnicodeUsernameValidator()],
        error_messages={"unique": "Ce nom d’utilisateur est déjà pris."},
        help_text="30 caractères maximum : lettres, chiffres et @ . + - _",
    )
    email = models.EmailField("E-mail de récupération (facultatif)", blank=True)

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta(AbstractUser.Meta):
        constraints = [models.UniqueConstraint(Lower("username"), name="unique_username_ci")]
