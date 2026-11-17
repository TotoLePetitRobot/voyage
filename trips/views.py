from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from .forms import TripForm, MemberForm
from .models import Member, Trip

def trip_list(request):
    trips = []
    if request.user.is_authenticated:
        trips = Trip.objects.filter(members__user=request.user)
    return render(request, "trips/trip_list.html", {"trips": trips})

@login_required
def trip_create(request):
    form = TripForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        trip = form.save()
        Member.objects.create(trip=trip, user=request.user, is_organizer=True)
        return redirect("trip_detail", pk=trip.pk)
    return render(request, "trips/trip_form.html", {"form": form})

@login_required
def trip_detail(request, pk):
    trip = get_object_or_404(Trip, pk=pk, members__user=request.user)
    me = trip.members.get(user=request.user)
    form = MemberForm(request.POST or None, instance=me)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("trip_detail", pk=trip.pk)
    invite_url = request.build_absolute_uri(reverse("trip_join", args=[trip.invite_token]))
    return render(request, "trips/trip_detail.html", {
        "trip": trip,
        "members": trip.members.select_related("user"),
        "form": form,
        "invite_url": invite_url,
    })

@login_required
def trip_join(request, token):
    trip = get_object_or_404(Trip, invite_token=token)
    Member.objects.get_or_create(trip=trip, user=request.user)
    return redirect("trip_detail", pk=trip.pk)