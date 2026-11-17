import secrets
from django.conf import settings
from django.db import models

def make_token():
    return secrets.token_urlsafe(16)

class Trip(models.Model):
    name = models.CharField("Nom du voyage", max_length=120)
    invite_token = models.CharField(max_length=32, unique=True, default=make_token)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Member(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="memberships")
    display_name = models.CharField("Nom affiché", max_length=60, blank=True)
    origin_city = models.CharField("Ville de départ", max_length=100, blank=True)
    is_organizer = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["trip", "user"], name="unique_member_per_trip")
        ]

    @property
    def label(self):
        return self.display_name or self.user.email.split("@")[0]

    def __str__(self):
        return f"{self.label} – {self.trip}"