from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import DestinationForm, ImportForm, MemberForm, TripForm
from .importers import parse_destinations
from .models import Destination, Member, Trip


def get_trip_and_member(request, pk):
    trip = get_object_or_404(Trip, pk=pk, members__user=request.user)
    return trip, trip.members.get(user=request.user)


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
    trip, me = get_trip_and_member(request, pk)
    form = MemberForm(request.POST or None, instance=me)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("trip_detail", pk=trip.pk)
    invite_url = request.build_absolute_uri(reverse("trip_join", args=[trip.invite_token]))
    return render(request, "trips/trip_detail.html", {
        "trip": trip,
        "me": me,
        "members": trip.members.select_related("user"),
        "destinations": trip.destinations.select_related("proposed_by__user"),
        "form": form,
        "invite_url": invite_url,
    })


@login_required
def trip_join(request, token):
    trip = get_object_or_404(Trip, invite_token=token)
    Member.objects.get_or_create(trip=trip, user=request.user)
    return redirect("trip_detail", pk=trip.pk)


@login_required
def destination_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    form = DestinationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        dest = form.save(commit=False)
        dest.trip = trip
        dest.proposed_by = me
        dest.save()
        return redirect("trip_detail", pk=trip.pk)
    return render(request, "trips/destination_form.html",
                  {"form": form, "trip": trip, "title": "Nouvelle destination"})


@login_required
def destination_edit(request, dest_pk):
    dest = get_object_or_404(Destination, pk=dest_pk, trip__members__user=request.user)
    form = DestinationForm(request.POST or None, instance=dest)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("trip_detail", pk=dest.trip_id)
    return render(request, "trips/destination_form.html",
                  {"form": form, "trip": dest.trip, "title": f"Modifier : {dest.name}"})


@login_required
@require_POST
def destination_delete(request, dest_pk):
    dest = get_object_or_404(Destination, pk=dest_pk, trip__members__user=request.user)
    me = dest.trip.members.get(user=request.user)
    if not (me.is_organizer or dest.proposed_by_id == me.id):
        raise PermissionDenied
    trip_pk = dest.trip_id
    dest.delete()
    return redirect("trip_detail", pk=trip_pk)


@login_required
def destination_import(request, pk):
    trip, me = get_trip_and_member(request, pk)
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        raw = form.cleaned_data["file"].read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        try:
            rows = parse_destinations(text)
        except ValueError as e:
            form.add_error("file", str(e))
        else:
            existing = {n.lower() for n in trip.destinations.values_list("name", flat=True)}
            new = []
            for r in rows:
                key = r["name"].lower()
                if key in existing:
                    continue
                existing.add(key)
                new.append(Destination(trip=trip, proposed_by=me, name=r["name"],
                                       url=r["url"], notes=r["notes"], extra_info=r["extra"]))
            Destination.objects.bulk_create(new)
            messages.success(
                request,
                f"{len(new)} destination(s) importée(s), {len(rows) - len(new)} ignorée(s) (déjà présentes).",
            )
            return redirect("trip_detail", pk=trip.pk)
    return render(request, "trips/destination_import.html", {"form": form, "trip": trip})
