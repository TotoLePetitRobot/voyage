from decimal import Decimal
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
                    LegForm)
from .importers import parse_destinations
from .links import (SNCF_CONNECT_URL, lodging_links, sncf_phrase, train_links)
from .middleware import PseudoRequired
from .models import (CATEGORY_CHOICES, DateOption, DateVote, Destination, DestVote, 
                     Expense, ExpenseShare, Meal, MealItem, Member, SolidarityPref, 
                     Trip, Leg, DIRECTION_CHOICES, ROLE_CHOICES)
from .settlement import (compute_balances, compute_owed, suggest_transfers, 
                         to_cents, to_milli)
from .shopping import aggregate

def get_trip_and_member(request, pk, require_pseudo=True):
    trip = get_object_or_404(Trip, pk=pk, members__user=request.user)
    me = trip.members.get(user=request.user)
    if require_pseudo and not me.display_name.strip():
        raise PseudoRequired(trip.pk)
    return trip, me


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
    if not (me.can_manage or dest.proposed_by_id == me.id):
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
                                       nearest_station=r["station"], url=r["url"],
                                       notes=r["notes"], extra_info=r["extra"]))
            Destination.objects.bulk_create(new)
            messages.success(
                request,
                f"{len(new)} destination(s) importée(s), {len(rows) - len(new)} ignorée(s) (déjà présentes).",
            )
            return redirect("trip_detail", pk=trip.pk)
    return render(request, "trips/destination_import.html", {"form": form, "trip": trip})

MAX_DATE_OPTIONS = 26
MIN_ANONYMOUS_FOR_STATS = 3


@login_required
def date_list(request, pk):
    trip, me = get_trip_and_member(request, pk)
    options = list(trip.date_options.select_related("proposed_by__user"))[:MAX_DATE_OPTIONS]
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
    months, legend, truncated = build_calendar(options)
    return render(request, "trips/dates.html", {
        "trip": trip, "me": me, "form": form,
        "months": months, "legend": legend, "truncated": truncated,
    })


@login_required
@require_POST
def date_delete(request, opt_pk):
    opt = get_object_or_404(DateOption, pk=opt_pk, trip__members__user=request.user)
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


@login_required
def trip_summary(request, pk):
    trip, me = get_trip_and_member(request, pk)
    members = list(trip.members.select_related("user"))
    named, anon_count, anon_stats = budget_summary(members)
    return render(request, "trips/summary.html", {
        "trip": trip,
        "destinations": with_scores(trip.destinations.all()).order_by("-total", "-yes", "name"),
        "dates": with_scores(trip.date_options.all()).order_by("-total", "-yes", "start_date"),
        "named_budgets": named,
        "anon_count": anon_count,
        "anon_stats": anon_stats,
        "missing_budget": sum(1 for m in members if m.budget is None),
        "min_anon": MIN_ANONYMOUS_FOR_STATS,
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


@login_required
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
        e.can_edit = me.can_manage or e.created_by_id == me.pk
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
                  {"trip": trip, "layers": shown, "my_detail": my_detail})


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


@login_required
def expense_edit(request, expense_pk):
    expense = get_object_or_404(
        Expense.objects.exclude(hidden_from__user=request.user),
        pk=expense_pk, trip__members__user=request.user)
    trip = expense.trip
    me = trip.members.get(user=request.user)
    if expense.is_transfer or not (me.can_manage or expense.created_by_id == me.pk):
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
    expense = get_object_or_404(
        Expense.objects.exclude(hidden_from__user=request.user),
        pk=expense_pk, trip__members__user=request.user)
    me = expense.trip.members.get(user=request.user)
    if not (me.can_manage or expense.created_by_id == me.pk):
        raise PermissionDenied
    trip_pk = expense.trip_id
    expense.delete()
    return redirect("budget", pk=trip_pk)


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


@login_required
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
    meal = get_object_or_404(Meal, pk=meal_pk, trip__members__user=request.user)
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
    meal = get_object_or_404(Meal, pk=meal_pk, trip__members__user=request.user)
    trip, me = get_trip_and_member(request, meal.trip_id)
    if not me.can_edit_meal(meal):
        raise PermissionDenied
    meal.delete()
    return redirect("meals", pk=trip.pk)


@login_required
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


@login_required
def legs(request, pk):
    trip, me = get_trip_and_member(request, pk)
    all_legs = list(trip.legs.select_related("proposed_by").prefetch_related("passengers"))
    sections = []
    for code, label in DIRECTION_CHOICES:
        items = [leg for leg in all_legs if leg.direction == code]
        previous, total, has_price = None, Decimal("0"), False
        for leg in items:
            leg.can_edit = me.can_manage or leg.proposed_by_id == me.pk
            leg.is_passenger = any(p.pk == me.pk for p in leg.passengers.all())
            leg.can_join = (leg.is_car and not leg.is_passenger
                            and leg.proposed_by_id != me.pk and (leg.seats_left or 0) > 0)
            leg.warning = bool(previous and previous.arrival_at and leg.departure_at
                               and leg.departure_at < previous.arrival_at)
            if leg.price is not None:
                total += leg.price
                has_price = True
            previous = leg
        sections.append({"code": code, "label": label, "legs": items,
                         "total": total if has_price else None})
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
    initial = {"direction": direction, "transport": "train",
               "position": min(trip.legs.filter(direction=direction).count() + 1, 20)}
    form = LegForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        leg = form.save(commit=False)
        leg.trip, leg.proposed_by = trip, me
        leg.save()
        return redirect("legs", pk=trip.pk)
    return render(request, "trips/leg_form.html",
                  {"form": form, "trip": trip, "title": "Ajouter une étape"})


def _leg_for_member(request, leg_pk):
    leg = get_object_or_404(Leg, pk=leg_pk, trip__members__user=request.user)
    trip, me = get_trip_and_member(request, leg.trip_id)
    return leg, trip, me


@login_required
def leg_edit(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    if not (me.can_manage or leg.proposed_by_id == me.pk):
        raise PermissionDenied
    form = LegForm(request.POST or None, instance=leg)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("legs", pk=trip.pk)
    return render(request, "trips/leg_form.html",
                  {"form": form, "trip": trip, "title": f"Modifier : {leg.name}"})


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


@login_required
@require_POST
def leg_leave(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    leg.passengers.remove(me)
    messages.success(request, "Vous n’êtes plus passager de cette voiture.")
    return redirect("legs", pk=trip.pk)


@login_required
@require_POST
def leg_remove(request, leg_pk):
    leg, trip, me = _leg_for_member(request, leg_pk)
    if not (me.can_manage or leg.proposed_by_id == me.pk):
        raise PermissionDenied
    member_id = _int_or_none(request.POST.get("member_id"))
    if member_id is not None:
        leg.passengers.remove(*leg.passengers.filter(pk=member_id))
    return redirect("legs", pk=trip.pk)
