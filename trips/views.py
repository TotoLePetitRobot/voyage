from decimal import Decimal, InvalidOperation
from statistics import median

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .calendar_utils import build_calendar
from .forms import (DateOptionForm, DestinationForm, ExpenseForm, ImportForm, 
                    MealForm, MemberForm, PseudoForm, SolidarityForm, TripForm,
                    LegForm, LodgingForm, PollEditForm, PollForm)
from .importers import parse_destinations
from .leg_utils import leg_key, sort_key
from .links import (SNCF_CONNECT_URL, lodging_links, sncf_phrase, train_links)
from .middleware import LoginRedirect, PseudoRequired
from .models import (CATEGORY_CHOICES, DateOption, DateVote, Destination, DestVote, 
                     Expense, ExpenseShare, Meal, MealItem, Member, SolidarityPref, 
                     Trip, Leg, Lodging, DIRECTION_CHOICES, ROLE_CHOICES, Poll,
                     PollOption, PollVote, LegParticipant, TRIP_TABS, TRIP_TAB_LABELS)
from .periods import parse_periods
from .polls import tally, votes_from_post
from .ranking import summarize, top_pairs
from .settlement import (compute_balances, compute_owed, suggest_transfers, 
                         to_cents, to_milli)
from .shopping import aggregate
from .spectator import SPECTATOR_KEY, SPECTATOR_VIEWS, Spectator

def get_trip_and_member(request, pk, require_pseudo=True):
    # 1. Un membre connecté a toujours tous ses droits, même avec une session spectateur.
    if request.user.is_authenticated:
        trip = Trip.objects.filter(pk=pk, members__user=request.user).first()
        if trip is not None:
            me = trip.members.get(user=request.user)
            if require_pseudo and not me.display_name.strip():
                raise PseudoRequired(trip.pk)
            return trip, me
    # 2. Spectateur : lecture seule, uniquement les pages autorisées, voyage ouvert.
    if str(pk) in (request.session.get(SPECTATOR_KEY) or []):
        url_name = getattr(request.resolver_match, "url_name", None)
        if request.method not in ("GET", "HEAD") or url_name not in SPECTATOR_VIEWS:
            raise PermissionDenied
        request.spectator = True
        return get_object_or_404(Trip, pk=pk, allow_spectators=True), Spectator()
    # 3. Les autres
    if not request.user.is_authenticated:
        raise LoginRedirect
    raise Http404

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


#@login_required
def trip_detail(request, pk):
    trip, me = get_trip_and_member(request, pk)
    spectator = getattr(me, "is_spectator", False)
    form = None if spectator else MemberForm(request.POST or None, instance=me)
    if form is not None and request.method == "POST" and form.is_valid():
        form.save()
        return redirect("trip_detail", pk=trip.pk)
    invite_url = request.build_absolute_uri(reverse("trip_join", args=[trip.invite_token]))
    return render(request, "trips/trip_detail.html", {
        "trip": trip,
        "me": me,
        "members": trip.members.select_related("user"),
        "form": form,
        "invite_url": invite_url,
    })

def trip_join(request, token):
    trip = get_object_or_404(Trip, invite_token=token)
    if not request.user.is_authenticated:
        return render(request, "trips/join.html", {"trip": trip})
    mine = trip.members.filter(user=request.user).first()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "claim" and mine is None:
            target = trip.members.filter(
                pk=_int_or_none(request.POST.get("member_id")) or 0).first()
            if target is not None and target.is_virtual:
                target.user = request.user
                target.save(update_fields=["user"])
                mine = target
                messages.success(request,
                                 f"Vous occupez désormais la place de « {target.label} ».")
            else:
                messages.error(request, "Cette place n'existe pas ou est déjà prise.")
        elif action == "own" and mine is None:
            mine = Member.objects.create(trip=trip, user=request.user)
            return redirect("trip_pseudo", pk=trip.pk)
    if mine is not None:
        if not mine.display_name.strip():
            return redirect("trip_pseudo", pk=trip.pk)
        return redirect("trip_detail", pk=trip.pk)
    places = trip.members.filter(user=None).order_by("id")
    return render(request, "trips/join.html", {"trip": trip, "places": places})

def spectator_start(request, token):
    trip = get_object_or_404(Trip, invite_token=token, allow_spectators=True)
    watched = list(request.session.get(SPECTATOR_KEY) or [])
    if str(trip.pk) not in watched:
        request.session[SPECTATOR_KEY] = (watched + [str(trip.pk)])[-50:]
    return redirect("trip_detail", pk=trip.pk)


@require_POST
def spectator_stop(request, pk):
    request.session[SPECTATOR_KEY] = [
        p for p in (request.session.get(SPECTATOR_KEY) or []) if p != str(pk)]
    return redirect("trip_list")


@login_required
@require_POST
def spectators_toggle(request, pk):
    trip, me = get_trip_and_member(request, pk)
    if not me.can_manage:
        raise PermissionDenied
    trip.allow_spectators = not trip.allow_spectators
    trip.save(update_fields=["allow_spectators"])
    messages.success(request, "Le mode spectateur est activé." if trip.allow_spectators
                     else "Le mode spectateur est désactivé.")
    return redirect("trip_detail", pk=trip.pk)

@login_required
@require_POST
def virtual_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    if not me.can_manage:
        raise PermissionDenied
    name = " ".join((request.POST.get("display_name") or "").split())
    if not 2 <= len(name) <= 30:
        messages.error(request, "Nom : entre 2 et 30 caractères.")
    elif trip.members.filter(display_name__iexact=name).exists():
        messages.error(request, "Ce nom existe déjà dans ce voyage.")
    elif trip.members.count() >= 60:
        messages.error(request, "60 membres au maximum par voyage.")
    else:
        Member.objects.create(trip=trip, display_name=name)
        messages.success(request, f"Place « {name} » créée.")
    return redirect("trip_detail", pk=trip.pk)

@login_required
@require_POST
def virtual_delete(request, pk):
    trip, me = get_trip_and_member(request, pk)
    if not me.can_manage:
        raise PermissionDenied
    target = trip.members.filter(
        pk=_int_or_none(request.POST.get("member_id")) or 0).first()
    if target is None or not target.is_virtual:
        messages.error(request, "Ce membre n'est pas une place fictive.")
    elif target.expense_shares.exists() or target.paid_expenses.exists():
        messages.error(request, "Cette place est liée à des dépenses : réaffectez-les "
                                "ou supprimez-les d'abord.")
    else:
        with transaction.atomic():
            target.hidden_expenses.clear()
            target.solidarity_prefs.all().delete()
            trip.expenses.filter(created_by=target).update(created_by=None)
            target.delete()
        messages.success(request, "Place retirée.")
    return redirect("trip_detail", pk=trip.pk)


@login_required
def trip_pseudo(request, pk):
    trip, me = get_trip_and_member(request, pk, require_pseudo=False)
    form = PseudoForm(request.POST or None, instance=me)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("trip_detail", pk=trip.pk)
    return render(request, "trips/pseudo.html", {"form": form, "trip_name": trip.name})

@login_required
def destination_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    form = DestinationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        dest = form.save(commit=False)
        dest.trip = trip
        dest.proposed_by = me
        dest.save()
        return redirect(f"{reverse('destination_list', args=[trip.pk])}#dest-{dest.pk}")
    return render(request, "trips/destination_form.html",
                  {"form": form, "trip": trip, "title": "Nouvelle destination"})


@login_required
def destination_edit(request, dest_pk):
    dest = get_object_or_404(Destination, pk=dest_pk)
    form = DestinationForm(request.POST or None, instance=dest)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(f"{reverse('destination_list', args=[dest.trip_id])}#dest-{dest.pk}")
    return render(request, "trips/destination_form.html",
                  {"form": form, "trip": dest.trip, "title": f"Modifier : {dest.name}"})


@login_required
@require_POST
def destination_delete(request, dest_pk):
    dest = get_object_or_404(Destination, pk=dest_pk)
    me = dest.trip.members.get(user=request.user)
    if not (me.can_manage or dest.proposed_by_id == me.id):
        raise PermissionDenied
    trip_pk = dest.trip_id
    dest.delete()
    return redirect("destination_list", pk=trip_pk)


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
                                       nearest_station=r["station"], url=r["url"],
                                       notes=r["notes"], extra_info=r["extra"]))
            Destination.objects.bulk_create(new)
            messages.success(
                request,
                f"{len(new)} destination(s) importée(s), {len(rows) - len(new)} ignorée(s) (déjà présentes).",
            )
        return redirect("destination_list", pk=trip.pk)
    return render(request, "trips/destination_import.html", {"form": form, "trip": trip})

MAX_DATE_OPTIONS = 26
MIN_ANONYMOUS_FOR_STATS = 3

MONTH_CHOICES = [1, 2, 3, 4, 6, 9, 12, 18, 24]
DEFAULT_MONTHS = 3
MAX_CALENDAR_OFFSET = 24


def _calendar_window(request):
    """Nombre de mois affichés (mémorisé dans la session) et décalage du premier mois."""
    asked = _int_or_none(request.GET.get("mois"))
    if asked is not None and 1 <= asked <= 24:
        request.session["calendar_months"] = asked
        shown = asked
    else:
        stored = request.session.get("calendar_months")
        shown = stored if isinstance(stored, int) and 1 <= stored <= 24 else DEFAULT_MONTHS
    offset = _int_or_none(request.GET.get("decalage"))
    if offset is None or offset > MAX_CALENDAR_OFFSET:
        offset = 0
    return shown, offset


#@login_required
def date_list(request, pk):
    trip, me = get_trip_and_member(request, pk)
    options = list(trip.date_options.select_related("proposed_by"))[:MAX_DATE_OPTIONS]
    for opt in options:
        opt.can_edit = me.can_manage or opt.proposed_by_id == me.pk
    form = DateOptionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        start, end = form.cleaned_data["start_date"], form.cleaned_data["end_date"]
        if trip.date_options.count() >= MAX_DATE_OPTIONS:
            form.add_error(None, f"Maximum {MAX_DATE_OPTIONS} périodes par voyage.")
        elif trip.date_options.filter(start_date=start, end_date=end).exists():
            form.add_error(None, "Cette période existe déjà.")
        else:
            opt = form.save(commit=False)
            opt.trip, opt.proposed_by = trip, me
            opt.save()
            return redirect("date_list", pk=trip.pk)
    shown, offset = _calendar_window(request)
    months, legend = build_calendar(options, timezone.localdate(), shown, offset)
    return render(request, "trips/dates.html", {
        "trip": trip, "me": me, "form": form, "months": months, "legend": legend,
        "months_shown": shown,
        "month_choices": sorted(set(MONTH_CHOICES) | {shown}),
        "offset": offset,
        "prev_offset": offset - 1 if offset > 0 else None,
        "next_offset": offset + 1 if offset + 1 <= MAX_CALENDAR_OFFSET else None,
        "range_label": f"{months[0]['title']} – {months[-1]['title']}",
    })


@login_required
def date_edit(request, opt_pk):
    opt = get_object_or_404(DateOption, pk=opt_pk)
    trip, me = get_trip_and_member(request, opt.trip_id)
    if not (me.can_manage or opt.proposed_by_id == me.pk):
        raise PermissionDenied
    votes = opt.votes.count()
    form = DateOptionForm(request.POST or None, instance=opt, has_votes=votes > 0)
    if request.method == "POST" and form.is_valid():
        start, end = form.cleaned_data["start_date"], form.cleaned_data["end_date"]
        if trip.date_options.filter(start_date=start, end_date=end).exclude(pk=opt.pk).exists():
            form.add_error(None, "Cette période existe déjà.")
        else:
            with transaction.atomic():
                form.save()
                if form.cleaned_data.get("reset_votes"):
                    opt.votes.all().delete()
            messages.success(request, "Période modifiée.")
            return redirect("date_list", pk=trip.pk)
    return render(request, "trips/date_form.html", {"form": form, "trip": trip, "votes": votes})

@login_required
@require_POST
def date_delete(request, opt_pk):
    opt = get_object_or_404(DateOption, pk=opt_pk)
    me = opt.trip.members.get(user=request.user)
    if not (me.can_manage or opt.proposed_by_id == me.id):
        raise PermissionDenied
    trip_pk = opt.trip_id
    opt.delete()
    return redirect("date_list", pk=trip_pk)


def _save_votes(request, me, items, prefix, model, fk):
    for item in items:
        raw = request.POST.get(f"{prefix}_{item.pk}", "")
        if raw in ("0", "1", "2"):
            model.objects.update_or_create(member=me, **{fk: item}, defaults={"score": int(raw)})
        else:
            model.objects.filter(member=me, **{fk: item}).delete()


@login_required
def votes(request, pk):
    trip, me = get_trip_and_member(request, pk)
    destinations = list(trip.destinations.all())
    dates = list(trip.date_options.all())
    if request.method == "POST":
        _save_votes(request, me, destinations, "dest", DestVote, "destination")
        _save_votes(request, me, dates, "date", DateVote, "date_option")
        messages.success(request, "Vos votes sont enregistrés.")
        return redirect("trip_summary", pk=trip.pk)
    dest_scores = {v.destination_id: v.score for v in me.dest_votes.all()}
    date_scores = {v.date_option_id: v.score for v in me.date_votes.all()}
    for d in destinations:
        d.my_score = dest_scores.get(d.pk)
    for o in dates:
        o.my_score = date_scores.get(o.pk)
    return render(request, "trips/votes.html",
                  {"trip": trip, "destinations": destinations, "dates": dates})


def with_scores(qs):
    return qs.annotate(
        yes=Count("votes", filter=Q(votes__score=2)),
        maybe=Count("votes", filter=Q(votes__score=1)),
        no=Count("votes", filter=Q(votes__score=0)),
        total=Coalesce(Sum("votes__score"), 0),
    )


def budget_summary(members):
    named = [m for m in members if m.budget is not None and not m.budget_anonymous]
    anon = [m.budget for m in members if m.budget is not None and m.budget_anonymous]
    stats = None
    if len(anon) >= MIN_ANONYMOUS_FOR_STATS:
        stats = {"min": min(anon), "median": median(anon), "max": max(anon)}
    return named, len(anon), stats


#@login_required
def trip_summary(request, pk):
    trip, me = get_trip_and_member(request, pk)
    members = list(trip.members.select_related("user"))
    spectator = getattr(me, "is_spectator", False)
    named, anon_count, anon_stats = ([], 0, None) if spectator else budget_summary(members)
    return render(request, "trips/summary.html", {
        "trip": trip,
        "destinations": with_scores(trip.destinations.all()).order_by("-total", "-yes", "name"),
        "dates": with_scores(trip.date_options.all()).order_by("-total", "-yes", "start_date"),
        "named_budgets": named,
        "anon_count": anon_count,
        "anon_stats": anon_stats,
        "missing_budget": 0 if spectator else sum(1 for m in members if m.budget is None),
        "min_anon": MIN_ANONYMOUS_FOR_STATS,
        "podium": build_podium(trip),
        "max_score": 2 * len(members),
    })

def _build_layer(key, expenses, ids, pools, caps, extras):
    paid, base, transfer_owed = {}, {}, {}
    total = Decimal("0")
    for e in expenses:
        paid[e.paid_by_id] = paid.get(e.paid_by_id, 0) + to_cents(e.amount)
        if not e.is_transfer:
            total += e.amount
        for s in e.shares.all():
            cents = to_cents(s.amount)
            if e.is_transfer:
                transfer_owed[s.member_id] = transfer_owed.get(s.member_id, 0) + cents
            else:
                k = (e.category, s.member_id)
                base[k] = base.get(k, 0) + cents
    if key:  # registre caché : parts simples, sans règles de solidarité
        plain = {}
        for (_, member_id), cents in base.items():
            plain[member_id] = plain.get(member_id, 0) + cents
        base_owed = pooled = final = plain
    else:
        base_owed, pooled, final = compute_owed(base, pools, caps, extras)
    owed = {m: final.get(m, 0) + transfer_owed.get(m, 0) for m in ids}
    balances = compute_balances(ids, paid, owed)
    return {
        "key": key,
        "key_text": ",".join(str(i) for i in sorted(key)),
        "balances": balances,
        "transfers": suggest_transfers(balances),
        "total": total,
        "detail": {m: {"base": base_owed.get(m, 0), "pooled": pooled.get(m, 0),
                       "final": final.get(m, 0)} for m in ids},
    }


def trip_calc(trip):
    """Retourne (membres, registres). Le premier registre est le registre visible de tous ;
    les suivants sont les registres cachés, un par groupe de destinataires."""
    members = list(trip.members.select_related("user"))
    ids = [m.pk for m in members]
    groups = {frozenset(): []}
    for e in trip.expenses.prefetch_related("shares", "hidden_from"):
        groups.setdefault(frozenset(h.pk for h in e.hidden_from.all()), []).append(e)
    pools = {}
    for p in SolidarityPref.objects.filter(member__trip=trip):
        pools.setdefault(p.category, {})[p.member_id] = to_milli(p.weight)
    caps = {m.pk: to_cents(m.pay_cap) for m in members if m.pay_cap is not None}
    extras = {m.pk: to_cents(m.extra_max) for m in members if m.extra_max is not None}
    layers = [_build_layer(key, groups[key], ids, pools, caps, extras)
              for key in sorted(groups, key=lambda k: (len(k), sorted(k)))]
    return members, layers


def trip_balances(trip):
    members, layers = trip_calc(trip)
    return members, layers[0]["balances"], layers[0]["transfers"]


#@login_required
def budget(request, pk):
    trip, me = get_trip_and_member(request, pk)
    members, layers = trip_calc(trip)
    by_id = {m.pk: m for m in members}

    by_layer = {}
    expenses = trip.expenses.select_related("paid_by__user").prefetch_related(
        "shares__member__user", "hidden_from")
    for e in expenses:
        key = frozenset(h.pk for h in e.hidden_from.all())
        if me.pk in key:
            continue  # une dépense cachée à ce membre n'est jamais transmise au gabarit
        e.can_edit = me.can_edit_expense(e)
        by_layer.setdefault(key, []).append(e)

    shown = []
    for layer in layers:
        key = layer["key"]
        if me.pk in key:
            continue
        balances = [{"label": m.label, "cents": layer["balances"][m.pk],
                     "amount": Decimal(abs(layer["balances"][m.pk])) / 100}
                    for m in members if m.pk not in key]
        transfers = [{
            "payer": by_id[a].label, "payee": by_id[b].label,
            "payer_id": a, "payee_id": b, "cents": c, "amount": Decimal(c) / 100,
            "can_mark": me.can_manage or me.pk in (a, b),
        } for a, b, c in layer["transfers"]]
        shown.append({
            "hidden": bool(key), "key_text": layer["key_text"],
            "hidden_names": [by_id[i].label for i in sorted(key)],
            "balances": balances, "transfers": transfers,
            "expenses": by_layer.get(key, []), "total": layer["total"],
        })

    mine = layers[0]["detail"][me.pk]
    my_detail = None
    if mine["base"] != mine["pooled"] or mine["pooled"] != mine["final"]:
        my_detail = {k: Decimal(v) / 100 for k, v in mine.items()}
    return render(request, "trips/budget.html",
                  {"trip": trip, "layers": shown, "my_detail": my_detail, "me": me})


def _save_expense(form, trip, creator, expense=None):
    cd = form.cleaned_data
    created = expense is None
    with transaction.atomic():
        if created:
            expense = Expense(trip=trip, created_by=creator)
        expense.label = cd["label"]
        expense.category = cd["category"]
        expense.paid_by = cd["paid_by"]
        expense.amount = cd["amount"]
        expense.date = cd["date"]
        expense.split_mode = cd["split_mode"]
        expense.save()
        if created:
            expense.hidden_from.set(cd.get("hidden_from") or [])
        expense.shares.all().delete()
        ExpenseShare.objects.bulk_create([
            ExpenseShare(expense=expense, member_id=mid, amount=Decimal(c) / 100)
            for mid, c in cd["shares_cents"].items()
        ])
    return expense


@login_required
def expense_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    initial = {"paid_by": me.pk, "date": timezone.localdate(), "category": "autre",
               "participants": [m.pk for m in trip.members.all()]}
    form = ExpenseForm(request.POST or None, trip=trip, initial=initial, viewer=me)
    if request.method == "POST" and form.is_valid():
        _save_expense(form, trip, me)
        return redirect("budget", pk=trip.pk)
    return render(request, "trips/expense_form.html",
                  {"form": form, "trip": trip, "title": "Nouvelle dépense"})

def _expense_for(request, expense_pk):
    expense = get_object_or_404(Expense, pk=expense_pk)
    trip, me = get_trip_and_member(request, expense.trip_id)
    if request.user.is_authenticated and \
            expense.hidden_from.filter(user=request.user).exists():
        raise Http404
    return expense, trip, me

@login_required
def expense_edit(request, expense_pk):
    expense, trip, me = _expense_for(request, expense_pk)
    if expense.is_transfer or not me.can_edit_expense(expense):
        raise PermissionDenied
    hidden = list(expense.hidden_from.all())
    shares = {s.member_id: s.amount for s in expense.shares.all()}
    initial = {"label": expense.label, "amount": expense.amount, "date": expense.date,
               "category": expense.category, "paid_by": expense.paid_by_id,
               "split_mode": expense.split_mode, "participants": list(shares)}
    for mid, amt in shares.items():
        initial[f"share_{mid}"] = amt
    form = ExpenseForm(request.POST or None, trip=trip, initial=initial, viewer=me,
                       fixed_hidden={h.pk for h in hidden})
    if request.method == "POST" and form.is_valid():
        _save_expense(form, trip, me, expense)
        return redirect("budget", pk=trip.pk)
    return render(request, "trips/expense_form.html", {
        "form": form, "trip": trip, "title": f"Modifier : {expense.label}",
        "hidden_names": [h.label for h in hidden],
    })


@login_required
@require_POST
def expense_delete(request, expense_pk):
    expense, trip, me = _expense_for(request, expense_pk)
    if not me.can_edit_expense(expense):
        raise PermissionDenied
    trip_pk = expense.trip_id
    expense.delete()
    return redirect("budget", pk=trip_pk)

def _expense_for(request, expense_pk):
    expense = get_object_or_404(Expense, pk=expense_pk)
    trip, me = get_trip_and_member(request, expense.trip_id)
    if request.user.is_authenticated and \
            expense.hidden_from.filter(user=request.user).exists():
        raise Http404
    return expense, trip, me

@login_required
@require_POST
def settle(request, pk):
    trip, me = get_trip_and_member(request, pk)
    try:
        payer_id = int(request.POST["from_id"])
        payee_id = int(request.POST["to_id"])
        cents = int(request.POST["amount_cents"])
        key = frozenset(int(x) for x in request.POST.get("layer", "").split(",") if x)
    except (KeyError, ValueError):
        raise Http404
    if me.pk in key:
        raise Http404
    members, layers = trip_calc(trip)
    layer = next((l for l in layers if l["key"] == key), None)
    if layer is None or (payer_id, payee_id, cents) not in layer["transfers"]:
        raise Http404
    if not (me.can_manage or me.pk in (payer_id, payee_id)):
        raise PermissionDenied
    with transaction.atomic():
        e = Expense.objects.create(
            trip=trip, paid_by_id=payer_id, created_by=me, label="Remboursement",
            amount=Decimal(cents) / 100, split_mode="custom", is_transfer=True,
        )
        ExpenseShare.objects.create(expense=e, member_id=payee_id, amount=Decimal(cents) / 100)
        if key:
            e.hidden_from.set(Member.objects.filter(pk__in=key, trip=trip))
    messages.success(request, "Remboursement enregistré.")
    return redirect("budget", pk=trip.pk)

@login_required
def solidarity(request, pk):
    trip, me = get_trip_and_member(request, pk)
    form = SolidarityForm(request.POST or None, member=me,
                          initial={"pay_cap": me.pay_cap, "extra_max": me.extra_max})
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        with transaction.atomic():
            me.pay_cap, me.extra_max = cd["pay_cap"], cd["extra_max"]
            me.save(update_fields=["pay_cap", "extra_max"])
            for code, _ in CATEGORY_CHOICES:
                if cd.get(f"opt_{code}"):
                    SolidarityPref.objects.update_or_create(
                        member=me, category=code, defaults={"weight": cd[f"w_{code}"]})
                else:
                    SolidarityPref.objects.filter(member=me, category=code).delete()
        messages.success(request, "Vos choix sont enregistrés. Ils ne sont visibles que par vous.")
        return redirect("budget", pk=trip.pk)
    return render(request, "trips/solidarity.html", {
        "form": form, "trip": trip,
        "passwordless": request.user.check_password(""),
    })

@login_required
def search(request, pk, dest_pk):
    trip, me = get_trip_and_member(request, pk)
    dest = get_object_or_404(Destination, pk=dest_pk, trip=trip)

    options = list(with_scores(trip.date_options.all()).order_by("-total", "-yes", "start_date"))
    chosen = None
    pick = _int_or_none(request.GET.get("p"))
    if pick is not None:
        chosen = next((o for o in options if o.pk == pick), None)
    if chosen is None and options:
        chosen = options[0]

    members = list(trip.members.all())
    lodging, dated = lodging_links(
        dest.name,
        chosen.start_date if chosen else None,
        chosen.end_date if chosen else None,
        len(members),
    )

    def group(places):
        groups = {}
        for city in places:
            city = city.strip()
            if city:
                entry = groups.setdefault(city.casefold(), {"place": city, "count": 0})
                entry["count"] += 1
        return sorted(groups.values(), key=lambda g: g["place"].casefold())

    targets = dest.stations or [""]
    outward_day = chosen.start_date if chosen else None
    return_day = chosen.end_date if chosen else None

    outward = [{**g, "routes": [{
        "target": s or dest.name,
        "links": train_links(g["place"], dest.name, s),
        "sncf_phrase": sncf_phrase(g["place"], dest.name, s, outward_day),
    } for s in targets]} for g in group(m.origin_city for m in members)]

    back = [{**g, "routes": [{
        "source": s or dest.name,
        "links": train_links(s or dest.name, g["place"]),
        "sncf_phrase": sncf_phrase(s or dest.name, g["place"], "", return_day),
    } for s in targets]} for g in group(
        m.return_city.strip() or m.origin_city for m in members)]

    return render(request, "trips/search.html", {
        "trip": trip, "dest": dest, "options": options, "chosen": chosen,
        "lodging": lodging, "dated": dated, "adults": len(members),
        "outward": outward, "back": back, "sncf_url": SNCF_CONNECT_URL,
    })

def _int_or_none(value, max_len=6):
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= max_len else None


def _save_meal(form, trip, me):
    with transaction.atomic():
        meal = form.save(commit=False)
        if meal.pk is None:
            meal.trip, meal.proposed_by = trip, me
        meal.save()
        meal.items.all().delete()
        MealItem.objects.bulk_create([
            MealItem(meal=meal, name=i["name"], quantity=i["quantity"],
                     unit=i["unit"], position=n)
            for n, i in enumerate(form.items_parsed)
        ])
    return meal


#@login_required
def meals(request, pk):
    trip, me = get_trip_and_member(request, pk)
    days = {}
    for meal in trip.meals.select_related("proposed_by").prefetch_related("items"):
        meal.can_edit = me.can_edit_meal(meal)
        days.setdefault(meal.day, []).append(meal)
    return render(request, "trips/meals.html", {"trip": trip, "days": sorted(days.items())})


@login_required
def meal_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    form = MealForm(request.POST or None, initial={"day": 1, "meal_type": 2})
    if request.method == "POST" and form.is_valid():
        _save_meal(form, trip, me)
        return redirect("meals", pk=trip.pk)
    return render(request, "trips/meal_form.html",
                  {"form": form, "trip": trip, "title": "Proposer un repas"})


@login_required
def meal_edit(request, meal_pk):
    meal = get_object_or_404(Meal, pk=meal_pk)
    trip, me = get_trip_and_member(request, meal.trip_id)
    if not me.can_edit_meal(meal):
        raise PermissionDenied
    form = MealForm(request.POST or None, instance=meal)
    if request.method == "POST" and form.is_valid():
        _save_meal(form, trip, me)
        return redirect("meals", pk=trip.pk)
    return render(request, "trips/meal_form.html",
                  {"form": form, "trip": trip, "title": f"Modifier : {meal.title}"})


@login_required
@require_POST
def meal_delete(request, meal_pk):
    meal = get_object_or_404(Meal, pk=meal_pk)
    trip, me = get_trip_and_member(request, meal.trip_id)
    if not me.can_edit_meal(meal):
        raise PermissionDenied
    meal.delete()
    return redirect("meals", pk=trip.pk)


#@login_required
def meal_shopping(request, pk):
    trip, me = get_trip_and_member(request, pk)
    all_meals = list(trip.meals.prefetch_related("items"))
    ids = {i for i in (_int_or_none(v) for v in request.GET.getlist("m")) if i is not None}
    low, high = _int_or_none(request.GET.get("from")), _int_or_none(request.GET.get("to"))
    if ids:
        selected = [m for m in all_meals if m.pk in ids]
    elif low is not None or high is not None:
        low = 1 if low is None else low
        high = 10 ** 6 if high is None else high
        selected = [m for m in all_meals if low <= m.day <= high]
    else:
        selected = all_meals
    rows = [(f"J{m.day} {m.get_meal_type_display()} : {m.title}", i.name, i.quantity, i.unit)
            for m in selected for i in m.items.all()]
    shopping = aggregate(rows)
    return render(request, "trips/shopping.html", {
        "trip": trip, "selected": selected, "shopping": shopping,
        "text": "\n".join(f"- {s['line']}" for s in shopping),
    })

@login_required
def roles(request, pk):
    trip, me = get_trip_and_member(request, pk)
    if not me.can_manage:
        raise PermissionDenied
    assignable = me.assignable_roles()
    members = list(trip.members.order_by("id"))
    if request.method == "POST":
        with transaction.atomic():
            for m in members:
                if m.is_organizer:
                    continue
                posted = set(request.POST.getlist(f"roles_{m.pk}")) & assignable
                kept = {r for r in m.roles if r not in assignable}
                new_roles = sorted(kept | posted)
                if new_roles != sorted(m.roles):
                    m.roles = new_roles
                    m.save(update_fields=["roles"])
        messages.success(request, "Les rôles sont enregistrés.")
        return redirect("roles", pk=trip.pk)
    rows = [{
        "member": m,
        "boxes": [{"code": code, "label": label, "checked": code in m.roles,
                   "editable": code in assignable and not m.is_organizer}
                  for code, label in ROLE_CHOICES],
    } for m in members]
    return render(request, "trips/roles.html",
                  {"trip": trip, "rows": rows, "role_choices": ROLE_CHOICES})


MAX_LEGS = 40


def _identical_leg(trip, data, exclude_pk=None):
    wanted = leg_key(data["direction"], data["transport"], data["departure_place"],
                     data.get("departure_at"), data["arrival_place"], data.get("arrival_at"))
    for leg in trip.legs.exclude(transport="car"):
        key = leg_key(leg.direction, leg.transport, leg.departure_place, leg.departure_at,
                      leg.arrival_place, leg.arrival_at)
        if leg.pk != exclude_pk and key == wanted:
            return leg
    return None


def _join_leg(leg, member, price):
    participation, created = LegParticipant.objects.get_or_create(
        leg=leg, member=member, defaults={"price": price})
    if not created and price is not None:
        participation.price = price
        participation.save(update_fields=["price"])


def _drop_from_leg(leg, member):
    """Retire un membre d'un trajet. Un trajet hors voiture qui n'a plus de participant
    est supprimé (retourne True dans ce cas)."""
    if leg.is_car:
        leg.passengers.remove(member)
        return False
    removed = leg.participations.filter(member=member).delete()[0]
    if removed and not leg.participations.exists():
        leg.delete()
        return True
    return False


#@login_required
def legs(request, pk):
    trip, me = get_trip_and_member(request, pk)
    all_legs = sorted(
        trip.legs.select_related("proposed_by").prefetch_related(
            "passengers", "participations__member"),
        key=sort_key)
    sections = []
    for code, label in DIRECTION_CHOICES:
        items = [leg for leg in all_legs if leg.direction == code]
        for leg in items:
            leg.can_edit = me.can_manage or leg.proposed_by_id == me.pk
            if leg.is_car:
                leg.is_passenger = any(p.pk == me.pk for p in leg.passengers.all())
                leg.can_join = (not leg.is_passenger and leg.proposed_by_id != me.pk
                                and (leg.seats_left or 0) > 0)
            else:
                leg.crew = list(leg.participations.all())
                leg.my_participation = next(
                    (p for p in leg.crew if p.member_id == me.pk), None)
        sections.append({"code": code, "label": label, "legs": items})
    return render(request, "trips/legs.html", {"trip": trip, "sections": sections})


@login_required
def leg_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    if trip.legs.count() >= MAX_LEGS:
        messages.error(request, f"Maximum {MAX_LEGS} trajets par voyage.")
        return redirect("legs", pk=trip.pk)
    direction = request.GET.get("sens")
    if direction not in ("out", "back"):
        direction = "out"
    initial = {"direction": direction, "transport": "train"}
    source_leg = None
    source_pk = _int_or_none(request.GET.get("from"))
    if source_pk is not None:
        source_leg = trip.legs.exclude(transport="car").filter(pk=source_pk).first()
    if source_leg is not None:
        initial = {
            "direction": source_leg.direction, "name": source_leg.name,
            "transport": source_leg.transport, "status": source_leg.status,
            "departure_place": source_leg.departure_place,
            "departure_at": source_leg.departure_at,
            "arrival_place": source_leg.arrival_place, "arrival_at": source_leg.arrival_at,
        }
    form = LegForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if data["transport"] != "car":
            twin = _identical_leg(trip, data)
            if twin is not None:
                _join_leg(twin, me, data.get("price"))
                messages.success(request, "Ce trajet existe déjà : vous y avez été ajouté.")
                return redirect("legs", pk=trip.pk)
        leg = form.save(commit=False)
        leg.trip, leg.proposed_by = trip, me
        price = leg.price
        if leg.transport != "car":
            leg.price = None          # le prix des autres transports est personnel
        leg.save()
        if leg.transport != "car":
            LegParticipant.objects.create(leg=leg, member=me, price=price)
        return redirect("legs", pk=trip.pk)
    return render(request, "trips/leg_form.html", {
        "form": form, "trip": trip, "taking": source_leg is not None,
        "title": "Prendre ce trajet" if source_leg else "Ajouter un trajet"})


@login_required
def leg_edit(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    if not (me.can_manage or leg.proposed_by_id == me.pk):
        raise PermissionDenied
    form = LegForm(request.POST or None, instance=leg)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if (data["transport"] != "car"
                and _identical_leg(trip, data, exclude_pk=leg.pk) is not None):
            form.add_error(None, "Un trajet identique existe déjà : utilisez « Prendre "
                                 "ce trajet » sur celui-ci.")
        else:
            form.save()
            return redirect("legs", pk=trip.pk)
    return render(request, "trips/leg_form.html",
                  {"form": form, "trip": trip, "title": f"Modifier : {leg.name}"})


@login_required
@require_POST
def leg_price(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    participation = None if leg.is_car else leg.participations.filter(member=me).first()
    if participation is None:
        messages.error(request, "Vous ne participez pas à ce trajet.")
        return redirect("legs", pk=trip.pk)
    raw = request.POST.get("price", "").strip().replace(",", ".")
    if raw == "":
        participation.price = None
    else:
        try:
            value = Decimal(raw)
            if not value.is_finite():
                raise InvalidOperation
            value = value.quantize(Decimal("0.01"))
        except InvalidOperation:
            messages.error(request, "Prix invalide.")
            return redirect("legs", pk=trip.pk)
        if value < 0 or value > Decimal("100000"):
            messages.error(request, "Prix invalide.")
            return redirect("legs", pk=trip.pk)
        participation.price = value
    participation.save(update_fields=["price"])
    messages.success(request, "Votre prix est enregistré.")
    return redirect("legs", pk=trip.pk)


@login_required
@require_POST
def leg_leave(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    deleted = _drop_from_leg(leg, me)
    messages.success(request, "Ce trajet n’avait plus personne : il a été supprimé."
                     if deleted else "Vous n’êtes plus sur ce trajet.")
    return redirect("legs", pk=trip.pk)


@login_required
@require_POST
def leg_remove(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    if not (me.can_manage or leg.proposed_by_id == me.pk):
        raise PermissionDenied
    member_id = _int_or_none(request.POST.get("member_id"))
    member = trip.members.filter(pk=member_id).first() if member_id is not None else None
    if member is not None:
        _drop_from_leg(leg, member)
    return redirect("legs", pk=trip.pk)

def _leg_for_member(request, leg_pk):
    leg = get_object_or_404(Leg, pk=leg_pk)
    trip, me = get_trip_and_member(request, leg.trip_id)
    return leg, trip, me


@login_required
@require_POST
def leg_delete(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    if not (me.can_manage or leg.proposed_by_id == me.pk):
        raise PermissionDenied
    leg.delete()
    return redirect("legs", pk=trip.pk)


@login_required
@require_POST
def leg_join(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    with transaction.atomic():
        if not leg.is_car:
            messages.error(request, "Seules les voitures ont des passagers.")
        elif leg.proposed_by_id == me.pk:
            messages.error(request, "Vous conduisez déjà cette voiture.")
        elif leg.passengers.filter(pk=me.pk).exists():
            messages.info(request, "Vous êtes déjà passager de cette voiture.")
        elif leg.seats is None or leg.passengers.count() >= leg.seats:
            messages.error(request, "Il n’y a plus de place dans cette voiture.")
        else:
            leg.passengers.add(me)
            if leg.passengers.count() > leg.seats:  # deux inscriptions simultanées
                leg.passengers.remove(me)
                messages.error(request, "Il n’y a plus de place dans cette voiture.")
            else:
                messages.success(request, "Vous êtes ajouté comme passager.")
    return redirect("legs", pk=trip.pk)

def _decode_csv(raw):   
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def build_podium(trip, limit=3):
    destinations = list(trip.destinations.all())
    dates = list(trip.date_options.all())
    dest_votes = DestVote.objects.filter(destination__trip=trip).values_list("member_id", "destination_id", "score")
    date_votes = DateVote.objects.filter(date_option__trip=trip).values_list("member_id", "date_option_id", "score")
    dest_order, date_order = [d.pk for d in destinations], [o.pk for o in dates]
    ranked = top_pairs(dest_votes, date_votes,
                       dest_order, date_order, limit)
    by_dest = {d.pk: d for d in destinations}
    by_date = {o.pk: o for o in dates}
    if not ranked or all(p["score"] == 0 for p in ranked):
        return []
    return [{"rank": n, "destination": by_dest[p["dest"]], "period": by_date[p["date"]],
             "score": p["score"], "no": p["no"]} for n, p in enumerate(ranked, 1)]


@login_required
def date_import(request, pk):
    trip, me = get_trip_and_member(request, pk)
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        text = _decode_csv(form.cleaned_data["file"].read())
        try:
            periods, errors = parse_periods(text, timezone.localdate())
        except ValueError as error:
            form.add_error("file", str(error))
        else:
            existing = set(trip.date_options.values_list("start_date", "end_date"))
            new = [p for p in periods if p not in existing]
            duplicates = len(periods) - len(new)
            room = max(MAX_DATE_OPTIONS - len(existing), 0)
            kept, dropped = new[:room], new[room:]
            DateOption.objects.bulk_create([
                DateOption(trip=trip, start_date=s, end_date=e, proposed_by=me)
                for s, e in kept])
            summary = f"{len(kept)} période(s) importée(s)"
            if duplicates:
                summary += f", {duplicates} déjà présente(s)"
            messages.success(request, summary + ".")
            if dropped:
                messages.warning(request, f"{len(dropped)} période(s) non importée(s) : "
                                          f"maximum {MAX_DATE_OPTIONS} périodes par voyage.")
            for line in errors[:8]:
                messages.warning(request, line)
            if len(errors) > 8:
                messages.warning(request, f"… et {len(errors) - 8} autre(s) ligne(s) ignorée(s).")
            return redirect("date_list", pk=trip.pk)
    return render(request, "trips/dates_import.html", {"form": form, "trip": trip})


MAX_LODGINGS_PER_DESTINATION = 30


#@login_required
def lodgings(request, pk):
    trip, me = get_trip_and_member(request, pk)
    count = trip.members.count()
    sections = []
    for destination in trip.destinations.prefetch_related("lodgings__proposed_by"):
        items = list(destination.lodgings.all())
        for item in items:
            item.can_edit = me.can_edit_lodging(item)
            item.per_person = ((item.price / count).quantize(Decimal("0.01"))
                               if item.price is not None and count else None)
            item.too_small = item.capacity is not None and item.capacity < count
        sections.append({"destination": destination, "lodgings": items})
    return render(request, "trips/lodgings.html",
                  {"trip": trip, "sections": sections, "members_count": count})


@login_required
def lodging_add(request, dest_pk):
    destination = get_object_or_404(Destination, pk=dest_pk)
    trip, me = get_trip_and_member(request, destination.trip_id)
    if destination.lodgings.count() >= MAX_LODGINGS_PER_DESTINATION:
        messages.error(request, f"Maximum {MAX_LODGINGS_PER_DESTINATION} logements par destination.")
        return redirect("lodgings", pk=trip.pk)
    form = LodgingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        lodging = form.save(commit=False)
        lodging.destination, lodging.proposed_by = destination, me
        lodging.save()
        return redirect("lodgings", pk=trip.pk)
    return render(request, "trips/lodging_form.html", {
        "form": form, "trip": trip, "title": f"Proposer un logement : {destination.name}"})


def _lodging_for_member(request, lodging_pk):
    lodging = get_object_or_404(Lodging, pk=lodging_pk)
    trip, me = get_trip_and_member(request, lodging.destination.trip_id)
    if not me.can_edit_lodging(lodging):
        raise PermissionDenied
    return lodging, trip


@login_required
def lodging_edit(request, lodging_pk):
    lodging, trip = _lodging_for_member(request, lodging_pk)
    form = LodgingForm(request.POST or None, instance=lodging)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("lodgings", pk=trip.pk)
    return render(request, "trips/lodging_form.html",
                  {"form": form, "trip": trip, "title": f"Modifier : {lodging.name}"})


@login_required
@require_POST
def lodging_delete(request, lodging_pk):
    lodging, trip = _lodging_for_member(request, lodging_pk)
    lodging.delete()
    return redirect("lodgings", pk=trip.pk)


#@login_required
def destination_list(request, pk):
    trip, me = get_trip_and_member(request, pk)
    items = list(trip.destinations.select_related("proposed_by"))
    for d in items:
        d.can_delete = me.can_manage or d.proposed_by_id == me.pk
    return render(request, "trips/destinations.html", {"trip": trip, "destinations": items})

MAX_POLLS = 20
MAX_POLL_OPTIONS = 15


def _poll_for_member(request, poll_pk):
    poll = get_object_or_404(Poll, pk=poll_pk)
    trip, me = get_trip_and_member(request, poll.trip_id)
    return poll, trip, me

def _require_poll_manager(poll, me):
    if not (me.can_manage or poll.created_by_id == me.pk):
        raise PermissionDenied


#@login_required
def polls(request, pk):
    trip, me = get_trip_and_member(request, pk)
    items = trip.polls.select_related("created_by").annotate(
        voters=Count("options__votes__member", distinct=True))
    return render(request, "trips/polls.html", {"trip": trip, "polls": items})


@login_required
def poll_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    if trip.polls.count() >= MAX_POLLS:
        messages.error(request, f"Maximum {MAX_POLLS} sondages par voyage.")
        return redirect("polls", pk=trip.pk)
    form = PollForm(request.POST or None, initial={"kind": "single"})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            poll = form.save(commit=False)
            poll.trip, poll.created_by = trip, me
            poll.save()
            PollOption.objects.bulk_create([
                PollOption(poll=poll, text=text, position=n)
                for n, text in enumerate(form.options)])
        return redirect("poll_detail", poll_pk=poll.pk)
    return render(request, "trips/poll_form.html",
                  {"form": form, "trip": trip, "title": "Créer un sondage"})


#@login_required
def poll_detail(request, poll_pk):
    poll, trip, me = _poll_for_member(request, poll_pk)
    options = list(poll.options.all())
    ids = [o.pk for o in options]
    votes = list(PollVote.objects.filter(option__poll=poll)
                 .values_list("option_id", "member_id", "score"))
    results, voters = tally(poll.kind, ids, votes)
    by_id = {o.pk: o for o in options}
    for row in results:
        row["option"] = by_id[row["id"]]
    mine = {option: score for option, member, score in votes if member == me.pk}
    for o in options:
        o.my_score = mine.get(o.pk)
    return render(request, "trips/poll_detail.html", {
        "poll": poll, "trip": trip, "options": options, "results": results,
        "voters": voters, "has_vote": bool(mine),
        "can_edit": me.can_manage or poll.created_by_id == me.pk,
    })


@login_required
@require_POST
def poll_vote(request, poll_pk):
    poll, trip, me = _poll_for_member(request, poll_pk)
    if poll.is_closed:
        messages.error(request, "Ce sondage est clôturé.")
        return redirect("poll_detail", poll_pk=poll.pk)
    chosen = votes_from_post(poll.kind, request.POST, [o.pk for o in poll.options.all()])
    with transaction.atomic():
        PollVote.objects.filter(member=me, option__poll=poll).delete()
        PollVote.objects.bulk_create([
            PollVote(option_id=option, member=me, score=score)
            for option, score in chosen.items()])
    messages.success(request, "Votre vote est enregistré.")
    return redirect("poll_detail", poll_pk=poll.pk)


@login_required
def poll_edit(request, poll_pk):
    poll, trip, me = _poll_for_member(request, poll_pk)
    _require_poll_manager(poll, me)
    form = PollEditForm(request.POST or None, instance=poll)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("poll_detail", poll_pk=poll.pk)
    return render(request, "trips/poll_form.html",
                  {"form": form, "trip": trip, "title": "Modifier le sondage"})


@login_required
@require_POST
def poll_delete(request, poll_pk):
    poll, trip, me = _poll_for_member(request, poll_pk)
    _require_poll_manager(poll, me)
    poll.delete()
    return redirect("polls", pk=trip.pk)


@login_required
@require_POST
def poll_toggle(request, poll_pk):
    poll, trip, me = _poll_for_member(request, poll_pk)
    _require_poll_manager(poll, me)
    poll.is_closed = not poll.is_closed
    poll.save(update_fields=["is_closed"])
    return redirect("poll_detail", poll_pk=poll.pk)


@login_required
@require_POST
def poll_option_add(request, poll_pk):
    poll, trip, me = _poll_for_member(request, poll_pk)
    _require_poll_manager(poll, me)
    text = " ".join(request.POST.get("text", "").split())
    existing = list(poll.options.all())
    if poll.is_closed:
        messages.error(request, "Ce sondage est clôturé.")
    elif not text:
        messages.error(request, "Indiquez le texte de l’option.")
    elif len(text) > 120:
        messages.error(request, "120 caractères au maximum par option.")
    elif len(existing) >= MAX_POLL_OPTIONS:
        messages.error(request, f"{MAX_POLL_OPTIONS} options au maximum.")
    elif text.casefold() in {o.text.casefold() for o in existing}:
        messages.error(request, "Cette option existe déjà.")
    else:
        PollOption.objects.create(
            poll=poll, text=text,
            position=max((o.position for o in existing), default=-1) + 1)
        messages.success(request, "Option ajoutée.")
    return redirect("poll_detail", poll_pk=poll.pk)


@login_required
@require_POST
def poll_option_delete(request, option_pk):
    option = get_object_or_404(PollOption, pk=option_pk)
    poll = option.poll
    trip, me = get_trip_and_member(request, poll.trip_id)
    _require_poll_manager(poll, me)
    if poll.options.count() <= 2:
        messages.error(request, "Un sondage doit garder au moins deux options.")
    else:
        option.delete()
    return redirect("poll_detail", poll_pk=poll.pk)

@login_required
def trip_tabs(request, pk):
    trip, me = get_trip_and_member(request, pk)
    if not me.can_manage:
        raise PermissionDenied
    config = trip.tabs_config or {}
    if request.method == "POST":
        action = request.POST.get("action")
        tab = request.POST.get("tab")
        if tab in TRIP_TAB_LABELS:
            if action == "move":
                present = [n for n in config.get("order", []) if n in TRIP_TAB_LABELS] + \
                          [n for n, _, _, _ in TRIP_TABS if n not in config.get("order", [])]
                i = present.index(tab)
                j = i + (-1 if request.POST.get("dir") == "up" else 1)
                if 0 <= j < len(present):
                    present[i], present[j] = present[j], present[i]
                config["order"] = present
            elif action == "toggle":
                hidden = set(config.get("hidden", []))
                if tab in hidden:
                    hidden.discard(tab)
                else:
                    visible = [n for n in config.get("order",
                               list(TRIP_TAB_LABELS)) if n not in hidden]
                    if len([n for n in visible if n != tab]) < 1:
                        messages.error(request, "Au moins un onglet doit rester visible.")
                    else:
                        hidden.add(tab)
                config["hidden"] = sorted(hidden)
            trip.tabs_config = config
            trip.save(update_fields=["tabs_config"])
        return redirect("trip_tabs", pk=trip.pk)
    hidden = set(config.get("hidden", []))
    names = [n for n in config.get("order", []) if n in TRIP_TAB_LABELS] + \
            [n for n, _, _, _ in TRIP_TABS if n not in config.get("order", [])]
    rows = [{"name": n, "label": TRIP_TAB_LABELS[n], "hidden": n in hidden} for n in names]
    return render(request, "trips/tabs.html", {"trip": trip, "rows": rows})

