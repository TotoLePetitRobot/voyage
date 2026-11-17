from django.contrib import admin
from django.urls import path, include
from accounts.views import signup

urlpatterns = [
    path("admin/", admin.site.urls),
    path("comptes/inscription/", signup, name="signup"),
    path("comptes/", include("django.contrib.auth.urls")),
    path("", include("trips.urls")),
]