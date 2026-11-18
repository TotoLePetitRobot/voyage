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
                    MemberForm, SolidarityForm, TripForm)
from .importers import parse_destinations
from .links import lodging_links, train_links
from .models import (CATEGORY_CHOICES, DateOption, DateVote, Destination, DestVote, 
                     Expense, ExpenseShare, Member, SolidarityPref, Trip)
from .settlement import (compute_balances, compute_owed, suggest_transfers, 
                         to_cents, to_milli)

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
    if not (me.is_organizer or opt.proposed_by_id == me.id):
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

def trip_calc(trip):
    members = list(trip.members.select_related("user"))
    ids = [m.pk for m in members]
    paid = {}
    for row in trip.expenses.order_by().values("paid_by").annotate(t=Sum("amount")):
        paid[row["paid_by"]] = to_cents(row["t"])

    base, transfer_owed = {}, {}
    rows = (ExpenseShare.objects.filter(expense__trip=trip).order_by()
            .values("expense__category", "expense__is_transfer", "member")
            .annotate(t=Sum("amount")))
    for r in rows:
        cents = to_cents(r["t"])
        if r["expense__is_transfer"]:
            transfer_owed[r["member"]] = transfer_owed.get(r["member"], 0) + cents
        else:
            key = (r["expense__category"], r["member"])
            base[key] = base.get(key, 0) + cents

    pools = {}
    for p in SolidarityPref.objects.filter(member__trip=trip):
        pools.setdefault(p.category, {})[p.member_id] = to_milli(p.weight)
    caps = {m.pk: to_cents(m.pay_cap) for m in members if m.pay_cap is not None}
    extras = {m.pk: to_cents(m.extra_max) for m in members if m.extra_max is not None}

    base_owed, pooled, final = compute_owed(base, pools, caps, extras)
    owed = {m: final.get(m, 0) + transfer_owed.get(m, 0) for m in ids}
    balances = compute_balances(ids, paid, owed)
    detail = {m: {"base": base_owed.get(m, 0), "pooled": pooled.get(m, 0),
                  "final": final.get(m, 0)} for m in ids}
    return members, balances, suggest_transfers(balances), detail


def trip_balances(trip):
    members, balances, transfers, _ = trip_calc(trip)
    return members, balances, transfers

@login_required
def budget(request, pk):
    trip, me = get_trip_and_member(request, pk)
    members, balances, transfers, detail = trip_calc(trip)
    mine = detail[me.pk]
    my_detail = None
    if mine["base"] != mine["pooled"] or mine["pooled"] != mine["final"]:
        my_detail = {k: Decimal(v) / 100 for k, v in mine.items()}
    by_id = {m.pk: m for m in members}
    expenses = list(trip.expenses.select_related("paid_by__user")
                    .prefetch_related("shares__member__user"))
    for e in expenses:
        e.can_edit = me.is_organizer or e.created_by_id == me.pk
    rows = [{"label": m.label, "cents": balances[m.pk],
             "amount": Decimal(abs(balances[m.pk])) / 100} for m in members]
    transfer_rows = [{
        "payer": by_id[a].label, "payee": by_id[b].label,
        "payer_id": a, "payee_id": b, "cents": c, "amount": Decimal(c) / 100,
        "can_mark": me.is_organizer or me.pk in (a, b),
    } for a, b, c in transfers]
    total = sum((e.amount for e in expenses if not e.is_transfer), Decimal("0"))
    return render(request, "trips/budget.html", {
        "trip": trip, "balances": rows, "transfers": transfer_rows,
        "expenses": expenses, "total": total, "my_detail": my_detail,
    })


def _save_expense(form, trip, creator, expense=None):
    cd = form.cleaned_data
    with transaction.atomic():
        if expense is None:
            expense = Expense(trip=trip, created_by=creator)
        expense.label = cd["label"]
        expense.category = cd["category"]
        expense.paid_by = cd["paid_by"]
        expense.amount = cd["amount"]
        expense.date = cd["date"]
        expense.split_mode = cd["split_mode"]
        expense.save()
        expense.shares.all().delete()
        ExpenseShare.objects.bulk_create([
            ExpenseShare(expense=expense, member_id=mid, amount=Decimal(c) / 100)
            for mid, c in cd["shares_cents"].items()
        ])
    return expense


@login_required
def expense_add(request, pk):
    trip, me = get_trip_and_member(request, pk)
    initial = {"paid_by": me.pk, "date": timezone.localdate(),
               "participants": [m.pk for m in trip.members.all()]}
    form = ExpenseForm(request.POST or None, trip=trip, initial=initial)
    if request.method == "POST" and form.is_valid():
        _save_expense(form, trip, me)
        return redirect("budget", pk=trip.pk)
    return render(request, "trips/expense_form.html",
                  {"form": form, "trip": trip, "title": "Nouvelle dépense"})


@login_required
def expense_edit(request, expense_pk):
    expense = get_object_or_404(Expense, pk=expense_pk, trip__members__user=request.user)
    trip = expense.trip
    me = trip.members.get(user=request.user)
    if expense.is_transfer or not (me.is_organizer or expense.created_by_id == me.pk):
        raise PermissionDenied
    shares = {s.member_id: s.amount for s in expense.shares.all()}
    initial = {"label": expense.label, "amount": expense.amount, 
               "category": expense.category, "date": expense.date,
               "paid_by": expense.paid_by_id, "split_mode": expense.split_mode,
               "participants": list(shares)}
    for mid, amt in shares.items():
        initial[f"share_{mid}"] = amt
    form = ExpenseForm(request.POST or None, trip=trip, initial=initial)
    if request.method == "POST" and form.is_valid():
        _save_expense(form, trip, me, expense)
        return redirect("budget", pk=trip.pk)
    return render(request, "trips/expense_form.html",
                  {"form": form, "trip": trip, "title": f"Modifier : {expense.label}"})


@login_required
@require_POST
def expense_delete(request, expense_pk):
    expense = get_object_or_404(Expense, pk=expense_pk, trip__members__user=request.user)
    me = expense.trip.members.get(user=request.user)
    if not (me.is_organizer or expense.created_by_id == me.pk):
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
    except (KeyError, ValueError):
        raise Http404
    _, _, transfers = trip_balances(trip)
    if (payer_id, payee_id, cents) not in transfers:
        raise Http404
    if not (me.is_organizer or me.pk in (payer_id, payee_id)):
        raise PermissionDenied
    with transaction.atomic():
        e = Expense.objects.create(
            trip=trip, paid_by_id=payer_id, created_by=me, label="Remboursement",
            amount=Decimal(cents) / 100, split_mode="custom", is_transfer=True,
        )
        ExpenseShare.objects.create(expense=e, member_id=payee_id, amount=Decimal(cents) / 100)
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
    return render(request, "trips/solidarity.html", {"form": form, "trip": trip})

@login_required
def search(request, pk, dest_pk):
    trip, me = get_trip_and_member(request, pk)
    dest = get_object_or_404(Destination, pk=dest_pk, trip=trip)

    options = list(with_scores(trip.date_options.all()).order_by("-total", "-yes", "start_date"))
    chosen = None
    raw = request.GET.get("p", "")
    if raw.isdigit():
        chosen = next((o for o in options if o.pk == int(raw)), None)
    if chosen is None and options:
        chosen = options[0]

    members = list(trip.members.all())
    lodging, dated = lodging_links(
        dest.name,
        chosen.start_date if chosen else None,
        chosen.end_date if chosen else None,
        len(members),
    )

    origins = {}
    for m in members:
        city = m.origin_city.strip()
        if city:
            entry = origins.setdefault(city.casefold(), {"origin": city, "count": 0})
            entry["count"] += 1
    trains = [{**o, "links": train_links(o["origin"], dest.name)}
              for o in sorted(origins.values(), key=lambda o: o["origin"].casefold())]

    return render(request, "trips/search.html", {
        "trip": trip, "dest": dest, "options": options, "chosen": chosen,
        "lodging": lodging, "dated": dated, "adults": len(members), "trains": trains,
    })
