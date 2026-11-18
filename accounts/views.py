from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import ProfileForm, SignUpForm
from .models import User


def signup(request):
    next_url = request.GET.get("next") or request.POST.get("next") or "/"
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = "/"
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect(next_url)
    return render(request, "registration/signup.html", {"form": form, "next": next_url})


@login_required
def profile(request):
    user = User.objects.get(pk=request.user.pk)
    form = ProfileForm(request.POST or None, user=user, initial={"email": user.email})
    if request.method == "POST" and form.is_valid():
        form.save()
        update_session_auth_hash(request, user)
        messages.success(request, "Profil enregistré.")
        return redirect("profile")
    return render(request, "accounts/profile.html",
                  {"form": form, "has_password": form.has_password})

