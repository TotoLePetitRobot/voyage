from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import include, path
from django.views.generic import RedirectView

from accounts.forms import LoginForm
from accounts.views import profile, signup

urlpatterns = [
    path("admin/", admin.site.urls),
    path("comptes/inscription/", signup, name="signup"),
    path("comptes/profil/", profile, name="profile"),
    path("comptes/login/", LoginView.as_view(authentication_form=LoginForm), name="login"),
    path("comptes/password_change/", RedirectView.as_view(pattern_name="profile"),
         name="password_change"),
    path("comptes/", include("django.contrib.auth.urls")),
    path("", include("trips.urls")),
]
