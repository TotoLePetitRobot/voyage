import random
import re
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone as djtz

from .backup import backup_sqlite
from .calendar_utils import build_calendar
from .importers import parse_destinations
from .leg_utils import leg_key, normalize_place, sort_key
from .links import lodging_links, sncf_phrase, train_links
from .models import (DateOption, Destination, Expense, ExpenseShare, Meal, 
                     MealItem, Member, SolidarityPref, Trip, Leg, DateVote,
                     DestVote, Lodging, Poll, PollOption, PollVote, LegParticipant)
from .periods import parse_periods, check_period
from .polls import parse_options, tally, votes_from_post
from .ranking import summarize, top_pairs
from .settlement import allocate, compute_owed, equal_split, suggest_transfers
from .shopping import aggregate, format_line, parse_line
from .views import trip_balances, trip_calc

class SpectatorTests(TestCase):
    SECRETS = ["Cadeau secret", "4321,55", "Zurich", "77,77", "87,00"]

    def setUp(self):
        U = get_user_model()
        names = ("orga", "bob", "carl", "stranger")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test", allow_spectators=True)
        self.m = {}
        for n in names[:3]:
            self.m[n] = Member.objects.create(
                trip=self.trip, display_name=n.title(), user=self.users[n],
                is_organizer=(n == "orga"),
                origin_city="Zurich" if n == "bob" else "",
                budget=Decimal("4321.55") if n == "carl" else None)
        Destination.objects.create(trip=self.trip, name="Chamonix", proposed_by=self.m["bob"])
        DateOption.objects.create(trip=self.trip, start_date=date(2027, 2, 6),
                                  end_date=date(2027, 2, 13))
        leg = Leg.objects.create(trip=self.trip, name="Train", transport="train",
                                 departure_place="Paris", arrival_place="Chamonix",
                                 direction="out")
        LegParticipant.objects.create(leg=leg, member=self.m["bob"], price=Decimal("77.77"))
        gift = Expense.objects.create(trip=self.trip, paid_by=self.m["bob"],
                                      created_by=self.m["bob"], label="Cadeau secret",
                                      amount=Decimal("87"), split_mode="custom")
        ExpenseShare.objects.create(expense=gift, member=self.m["carl"], amount=Decimal("87"))
        gift.hidden_from.set([self.m["orga"]])
        pk = self.trip.pk
        self.public = [reverse(n, args=[pk]) for n in (
            "trip_detail", "destination_list", "date_list", "lodgings", "legs", "polls",
            "trip_summary", "meals", "meal_shopping")]
        self.private = [reverse(n, args=[pk]) for n in (
            "budget", "votes", "solidarity", "roles", "trip_tabs", "expense_add", "leg_add",
            "poll_add", "meal_add", "destination_add", "date_import", "destination_import")]

    def enter(self):
        r = self.client.get(reverse("spectator_start", args=[self.trip.invite_token]))
        self.assertRedirects(r, reverse("trip_detail", args=[self.trip.pk]))

    def test_anonymous_visitor_is_sent_to_login_without_spectator_mode(self):
        r = self.client.get(self.public[0])
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(reverse("login")))

    def test_spectator_mode_is_off_by_default_and_managers_can_toggle_it(self):
        other = Trip.objects.create(name="Fermé")
        r = self.client.get(reverse("spectator_start", args=[other.invite_token]))
        self.assertEqual(r.status_code, 404)
        url = reverse("spectators_toggle", args=[self.trip.pk])
        self.client.force_login(self.users["carl"])
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.users["orga"])
        self.client.post(url)
        self.trip.refresh_from_db()
        self.assertFalse(self.trip.allow_spectators)

    def test_disabling_cuts_existing_spectator_sessions(self):
        self.enter()
        self.assertEqual(self.client.get(self.public[1]).status_code, 200)
        Trip.objects.filter(pk=self.trip.pk).update(allow_spectators=False)
        self.assertEqual(self.client.get(self.public[1]).status_code, 404)

    def test_invalid_token_gives_404(self):
        self.assertEqual(self.client.get(reverse("spectator_start", args=["nope"])).status_code, 404)

    def test_spectator_reads_public_pages_with_a_banner(self):
        self.enter()
        for url in self.public:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(self.public[1]), "Chamonix")
        self.assertContains(self.client.get(self.public[0]), "Se connecter ou rejoindre")

    def test_no_private_data_reaches_spectators(self):
        self.enter()
        for url in self.public:
            html = self.client.get(url).content.decode()
            for secret in self.SECRETS:
                self.assertNotIn(secret, html, (url, secret))
        summary = self.client.get(self.public[6])
        self.assertNotContains(summary, "Budgets envisagés")

    def test_spectators_cannot_open_private_pages_nor_post_anywhere(self):
        self.enter()
        for url in self.private:
            self.assertIn(self.client.get(url).status_code, (302, 403), url)
        for url in self.public + self.private:
            self.assertIn(self.client.post(url, {"name": "Piège"}).status_code, (302, 403), url)
        self.assertEqual(Destination.objects.count(), 1)

    def test_spectator_tabs_hide_budget_and_votes(self):
        self.enter()
        html = self.client.get(self.public[0]).content.decode()
        nav = html.split('<nav class="tabs"')[1].split("</nav>")[0]
        self.assertNotIn(reverse("budget", args=[self.trip.pk]), nav)
        self.assertNotIn(reverse("votes", args=[self.trip.pk]), nav)
        self.assertIn(reverse("destination_list", args=[self.trip.pk]), nav)

    def test_spectator_mode_never_blocks_login_and_members_keep_their_rights(self):
        self.enter()
        r = self.client.post(reverse("login"), {"username": "orga", "password": "pw-test-123"})
        self.assertEqual(r.status_code, 302)
        r = self.client.post(reverse("destination_add", args=[self.trip.pk]), {
            "name": "Lisbonne", "nearest_station": "", "url": "", "notes": "", "extra_text": ""})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Destination.objects.filter(name="Lisbonne").exists())
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)

    def test_a_spectator_session_does_not_open_other_trips(self):
        other = Trip.objects.create(name="Autre", allow_spectators=True)
        self.enter()
        self.assertEqual(self.client.get(reverse("trip_detail", args=[other.pk])).status_code, 302)

    def test_logged_in_outsider_with_spectator_session_is_read_only(self):
        self.client.force_login(self.users["stranger"])
        self.enter()
        self.assertEqual(self.client.get(self.public[1]).status_code, 200)
        self.assertEqual(self.client.get(reverse("budget", args=[self.trip.pk])).status_code, 403)
        r = self.client.post(reverse("destination_add", args=[self.trip.pk]), {"name": "X"})
        self.assertEqual(r.status_code, 403)

    def test_stop_ends_spectator_mode(self):
        self.enter()
        self.client.post(reverse("spectator_stop", args=[self.trip.pk]))
        self.assertEqual(self.client.get(self.public[0]).status_code, 302)


class TabsConfigTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.orga = U.objects.create_user("orga", password="pw-test-123")
        self.simple = U.objects.create_user("simple", password="pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        Member.objects.create(trip=self.trip, display_name="Orga", user=self.orga,
                              is_organizer=True)
        Member.objects.create(trip=self.trip, display_name="Simple", user=self.simple)
        self.pk = self.trip.pk

    def nav(self, name="trip_detail", **kwargs):
        html = self.client.get(reverse(name, args=[self.pk])).content.decode()
        return html.split('<nav class="tabs"')[1].split("</nav>")[0]

    def test_hidden_and_reordered_tabs(self):
        self.trip.tabs_config = {"hidden": ["budget", "votes"], "order": ["meals", "legs"]}
        self.trip.save()
        self.client.force_login(self.simple)
        nav = self.nav()
        for hidden in ("budget", "votes"):
            self.assertNotIn(reverse(hidden, args=[self.pk]), nav)
        positions = [nav.index(f'href="{reverse(n, args=[self.pk])}"')
                     for n in ("meals", "legs", "trip_detail")]
        self.assertEqual(positions, sorted(positions))

    def test_only_managers_can_configure_tabs(self):
        url = reverse("trip_tabs", args=[self.pk])
        self.client.force_login(self.simple)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.orga)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"action": "toggle", "tab": "budget"})
        self.trip.refresh_from_db()
        self.assertIn("budget", self.trip.tabs_config["hidden"])

    def test_at_least_one_tab_stays_visible(self):
        from .models import TRIP_TAB_LABELS
        url = reverse("trip_tabs", args=[self.pk])
        self.client.force_login(self.orga)
        for name in TRIP_TAB_LABELS:
            self.client.post(url, {"action": "toggle", "tab": name})
        self.trip.refresh_from_db()
        self.assertEqual(len(TRIP_TAB_LABELS) - len(self.trip.tabs_config["hidden"]), 1)

    def test_tabs_appear_on_edit_pages_but_not_on_the_pseudo_page(self):
        member = Member.objects.get(user=self.simple)
        dest = Destination.objects.create(trip=self.trip, name="X", proposed_by=member)
        self.client.force_login(self.simple)
        page = self.client.get(reverse("destination_edit", args=[dest.pk]))
        self.assertContains(page, '<nav class="tabs"')
        Member.objects.filter(pk=member.pk).update(display_name="")
        page = self.client.get(reverse("trip_pseudo", args=[self.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, '<nav class="tabs"')


class SettlementTests(SimpleTestCase):
    def test_equal_split_spreads_extra_cents(self):
        shares = equal_split(1000, [3, 1, 2])
        self.assertEqual(shares, {1: 334, 2: 333, 3: 333})

    def test_equal_split_exact(self):
        self.assertEqual(equal_split(900, [1, 2, 3]), {1: 300, 2: 300, 3: 300})

    def test_simple_transfer(self):
        self.assertEqual(suggest_transfers({1: 1000, 2: -1000}), [(2, 1, 1000)])

    def test_transfers_settle_everyone(self):
        rng = random.Random(42)
        for _ in range(300):
            n = rng.randint(2, 8)
            values = [rng.randint(-5000, 5000) for _ in range(n - 1)]
            values.append(-sum(values))
            balances = {i + 1: v for i, v in enumerate(values)}
            transfers = suggest_transfers(balances)
            final = dict(balances)
            for payer, payee, amount in transfers:
                self.assertGreater(amount, 0)
                final[payer] += amount
                final[payee] -= amount
            self.assertTrue(all(v == 0 for v in final.values()))
            self.assertLessEqual(len(transfers), n - 1)


class BudgetViewTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.a = U.objects.create_user("a", password="pw-test-123")
        self.b = U.objects.create_user("b", password="pw-test-123")
        self.c = U.objects.create_user("c", password="pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        self.ma = Member.objects.create(trip=self.trip, display_name="Pseudo", user=self.a, is_organizer=True)
        self.mb = Member.objects.create(trip=self.trip, display_name="Pseudo", user=self.b)

    def add_expense(self, amount="30.00"):
        return self.client.post(reverse("expense_add", args=[self.trip.pk]), {
            "label": "Courses", "amount": amount, "date": "2026-11-20",
            "category": "autre",
            "paid_by": self.ma.pk, "split_mode": "equal",
            "participants": [self.ma.pk, self.mb.pk],
        })

    def test_equal_expense_gives_balances(self):
        self.client.force_login(self.a)
        self.assertEqual(self.add_expense().status_code, 302)
        _, balances, transfers = trip_balances(self.trip)
        self.assertEqual(balances, {self.ma.pk: 1500, self.mb.pk: -1500})
        self.assertEqual(transfers, [(self.mb.pk, self.ma.pk, 1500)])

    def test_custom_split_must_match_total(self):
        self.client.force_login(self.a)
        r = self.client.post(reverse("expense_add", args=[self.trip.pk]), {
            "label": "Resto", "amount": "50.00", "date": "2026-11-20",
            "paid_by": self.ma.pk, "split_mode": "custom",
            f"share_{self.ma.pk}": "20.00", f"share_{self.mb.pk}": "20.00",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Expense.objects.count(), 0)

    def test_outsider_cannot_see_budget(self):
        self.client.force_login(self.c)
        r = self.client.get(reverse("budget", args=[self.trip.pk]))
        self.assertEqual(r.status_code, 404)

    def test_settle_rejects_forged_amount_and_accepts_exact(self):
        self.client.force_login(self.a)
        self.add_expense()
        url = reverse("settle", args=[self.trip.pk])
        data = {"from_id": self.mb.pk, "to_id": self.ma.pk}
        self.assertEqual(self.client.post(url, {**data, "amount_cents": 1}).status_code, 404)
        self.assertEqual(self.client.post(url, {**data, "amount_cents": 1500}).status_code, 302)
        self.assertEqual(trip_balances(self.trip)[2], [])

class EquityLogicTests(SimpleTestCase):
    def test_allocate_is_exact(self):
        self.assertEqual(allocate(1000, {1: 1000, 2: 1000, 3: 1000}), {1: 334, 2: 333, 3: 333})

    def test_weighted_housing(self):
        base = {("logement", m): 20000 for m in (1, 2, 3)}
        _, pooled, _ = compute_owed(base, {"logement": {1: 1200, 2: 800}}, {}, {})
        self.assertEqual(pooled, {1: 24000, 2: 16000, 3: 20000})

    def test_cap_covered_by_volunteer(self):
        base = {("autre", m): 10000 for m in (1, 2, 3)}
        _, _, final = compute_owed(base, {}, {3: 4000}, {1: 10000})
        self.assertEqual(final, {1: 16000, 2: 10000, 3: 4000})

    def test_cap_with_insufficient_offers(self):
        base = {("autre", m): 20000 for m in (1, 2, 3)}
        _, _, final = compute_owed(base, {}, {3: 10000}, {1: 3000})
        self.assertEqual(final, {1: 23000, 2: 20000, 3: 17000})

    def test_totals_always_preserved(self):
        rng = random.Random(11)
        for _ in range(500):
            ids = list(range(1, rng.randint(2, 7) + 1))
            base = {(c, m): rng.randint(0, 50000) for c in "abc" for m in ids}
            pools = {c: {m: rng.randint(100, 3000) for m in ids if rng.random() < .6} for c in "abc"}
            pools = {c: w for c, w in pools.items() if w}
            caps = {m: rng.randint(0, 80000) for m in ids if rng.random() < .4}
            extras = {m: rng.randint(0, 40000) for m in ids if rng.random() < .4 and m not in caps}
            _, _, final = compute_owed(base, pools, caps, extras)
            self.assertEqual(sum(final.values()), sum(base.values()))
            self.assertTrue(all(v >= 0 for v in final.values()))


class EquityBalanceTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.trip = Trip.objects.create(name="Test")
        self.m = []
        for i, name in enumerate("abc"):
            user = U.objects.create_user(name, password="pw-test-123")
            self.m.append(Member.objects.create(trip=self.trip, display_name="Pseudo", user=user, is_organizer=(i == 0)))

    def expense(self, payer, amount, category, shares):
        e = Expense.objects.create(trip=self.trip, paid_by=payer, created_by=payer,
                                   label="x", amount=Decimal(amount), category=category,
                                   split_mode="custom")
        for member, share in shares.items():
            ExpenseShare.objects.create(expense=e, member=member, amount=Decimal(share))

    def test_transport_pooling_between_two_members(self):
        a, b, c = self.m
        self.expense(a, "100", "transport", {a: "100"})
        self.expense(b, "300", "transport", {b: "300"})
        SolidarityPref.objects.create(member=a, category="transport", weight=Decimal("1"))
        SolidarityPref.objects.create(member=b, category="transport", weight=Decimal("1"))
        _, balances, _ = trip_balances(self.trip)
        self.assertEqual(balances, {a.pk: -10000, b.pk: 10000, c.pk: 0})

    def test_weights_only_affect_opted_in_members(self):
        a, b, c = self.m
        self.expense(a, "600", "logement", {a: "200", b: "200", c: "200"})
        SolidarityPref.objects.create(member=a, category="logement", weight=Decimal("1.2"))
        SolidarityPref.objects.create(member=b, category="logement", weight=Decimal("0.8"))
        _, balances, _ = trip_balances(self.trip)
        self.assertEqual(balances, {a.pk: 36000, b.pk: -16000, c.pk: -20000})

    def test_cap_and_volunteer(self):
        a, b, c = self.m
        self.expense(a, "300", "autre", {a: "100", b: "100", c: "100"})
        c.pay_cap = Decimal("40"); c.save()
        a.extra_max = Decimal("100"); a.save()
        _, balances, _ = trip_balances(self.trip)
        self.assertEqual(balances, {a.pk: 14000, b.pk: -10000, c.pk: -4000})

class LinksTests(SimpleTestCase):
    def test_booking_link_with_dates(self):
        links, dated = lodging_links("La Plagne", date(2027, 2, 6), date(2027, 2, 13), 4)
        self.assertTrue(dated)
        url = links[0]["url"]
        for part in ("ss=La%20Plagne", "checkin=2027-02-06",
                     "checkout=2027-02-13", "group_adults=4"):
            self.assertIn(part, url)

    def test_special_characters_are_encoded(self):
        links, _ = lodging_links("Rock & Roll/Bar?x=1", None, None, 2)
        for link in links:
            self.assertIn("Rock%20%26%20Roll%2FBar%3Fx%3D1", link["url"])

    def test_zero_night_period_gives_undated_links(self):
        links, dated = lodging_links("Lisbonne", date(2027, 3, 1), date(2027, 3, 1), 2)
        self.assertFalse(dated)
        self.assertNotIn("checkin", links[0]["url"])

    def test_adults_are_clamped(self):
        self.assertIn("group_adults=1", lodging_links("X", None, None, 0)[0][0]["url"])
        self.assertIn("group_adults=30", lodging_links("X", None, None, 99)[0][0]["url"])

    def test_train_link(self):
        url = train_links("Saint-Étienne", "Chamonix")[0]["url"]
        self.assertIn("origin=Saint-%C3%89tienne", url)
        self.assertIn("travelmode=transit", url)

    def test_station_replaces_destination_in_train_link(self):
        url = train_links("Lyon", "Chamonix", "Gare de Chamonix-Mont-Blanc")[0]["url"]
        self.assertIn("destination=Gare%20de%20Chamonix-Mont-Blanc", url)

    def test_sncf_phrase(self):
        self.assertEqual(sncf_phrase("Lyon", "Chamonix", "Gare de Chamonix", date(2027, 2, 6)),
                         "Lyon Gare de Chamonix le 6 février")
        self.assertEqual(sncf_phrase("Lyon", "Chamonix"), "Lyon Chamonix")

class SearchViewTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.a = U.objects.create_user("a", password="pw-test-123")
        self.outsider = U.objects.create_user("z", password="pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        Member.objects.create(trip=self.trip, display_name="Pseudo", user=self.a, origin_city="Lyon")
        self.dest = Destination.objects.create(trip=self.trip, name="Chamonix", 
                                               nearest_station="Chamonix-Mont-Blanc")
        DateOption.objects.create(trip=self.trip, start_date=date(2027, 2, 6),
                                  end_date=date(2027, 2, 13))
        self.url = reverse("search", args=[self.trip.pk, self.dest.pk])

    def test_member_sees_prefilled_links(self):
        self.client.force_login(self.a)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "checkin=2027-02-06")
        self.assertContains(r, "origin=Lyon")

    def test_outsider_gets_404(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_page_shows_sncf_phrase_with_station(self):
        self.client.force_login(self.a)
        r = self.client.get(self.url)
        self.assertContains(r, "Lyon Chamonix-Mont-Blanc le 6 février")

class BackupTests(SimpleTestCase):
    def test_copy_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            con = sqlite3.connect(db)
            con.execute("create table t(x)")
            con.execute("insert into t values (42)")
            con.commit()
            con.close()
            for i in range(5):
                backup_sqlite(db, Path(tmp) / "b", keep=3, now=datetime(2026, 11, 18, 10, 0, i))
            files = sorted((Path(tmp) / "b").glob("db-*.sqlite3"))
            self.assertEqual(len(files), 3)
            self.assertTrue(files[-1].name.endswith("100004.sqlite3"))
            con = sqlite3.connect(files[-1])
            self.assertEqual(con.execute("select x from t").fetchone(), (42,))
            con.close()

    def test_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                backup_sqlite(Path(tmp) / "nope.sqlite3", tmp)

class PseudoTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.a = U.objects.create_user("anna", password="pw-test-123")
        self.b = U.objects.create_user("secretname", password="pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        self.ma = Member.objects.create(trip=self.trip, user=self.a)
        self.mb = Member.objects.create(trip=self.trip, user=self.b, display_name="Bob",
                                        budget=Decimal("100"))

    def test_member_without_pseudo_is_redirected(self):
        self.client.force_login(self.a)
        r = self.client.get(reverse("trip_detail", args=[self.trip.pk]))
        self.assertRedirects(r, reverse("trip_pseudo", args=[self.trip.pk]))

    def test_setting_pseudo_unlocks_the_trip(self):
        self.client.force_login(self.a)
        r = self.client.post(reverse("trip_pseudo", args=[self.trip.pk]), {"display_name": "Anna"})
        self.assertRedirects(r, reverse("trip_detail", args=[self.trip.pk]))
        self.assertEqual(self.client.get(reverse("budget", args=[self.trip.pk])).status_code, 200)

    def test_pseudo_must_be_unique_ignoring_case(self):
        self.client.force_login(self.a)
        r = self.client.post(reverse("trip_pseudo", args=[self.trip.pk]), {"display_name": "bob"})
        self.assertEqual(r.status_code, 200)
        self.ma.refresh_from_db()
        self.assertEqual(self.ma.display_name, "")

    def test_username_is_never_shown_to_others(self):
        self.ma.display_name = "Anna"
        self.ma.save()
        self.client.force_login(self.a)
        for name in ("trip_detail", "budget", "trip_summary"):
            r = self.client.get(reverse(name, args=[self.trip.pk]))
            self.assertContains(r, "Bob")
            self.assertNotContains(r, "secretname")


class StationsTests(SimpleTestCase):
    def test_stations_property(self):
        d = Destination(name="X", nearest_station=" Grenoble\n\nvalence tgv \ngrenoble")
        self.assertEqual(d.stations, ["Grenoble", "valence tgv"])

    def test_csv_import_splits_stations(self):
        rows = parse_destinations("nom;gare\nVillard-de-Lans;Grenoble|Valence TGV\n")
        self.assertEqual(rows[0]["station"], "Grenoble\nValence TGV")


class MultiStationSearchTests(TestCase):
    def test_several_stations_and_return_place(self):
        U = get_user_model()
        user = U.objects.create_user("anna", password="pw-test-123")
        trip = Trip.objects.create(name="Test")
        Member.objects.create(trip=trip, user=user, display_name="Anna",
                              origin_city="Lyon", return_city="Paris")
        dest = Destination.objects.create(trip=trip, name="Villard-de-Lans",
                                          nearest_station="Grenoble\nValence TGV")
        DateOption.objects.create(trip=trip, start_date=date(2027, 2, 6),
                                  end_date=date(2027, 2, 13))
        self.client.force_login(user)
        r = self.client.get(reverse("search", args=[trip.pk, dest.pk]))
        self.assertContains(r, "Lyon Grenoble le 6 février")
        self.assertContains(r, "Lyon Valence TGV le 6 février")
        self.assertContains(r, "Grenoble Paris le 13 février")
        self.assertContains(r, "Valence TGV Paris le 13 février")

D = Decimal


class ShoppingLogicTests(SimpleTestCase):
    def test_parse_line(self):
        self.assertIsNone(parse_line("   "))
        self.assertEqual(parse_line("500 g de riz"),
                         {"name": "riz", "quantity": D("500.000"), "unit": "g"})
        self.assertEqual(parse_line("500g riz")["unit"], "g")
        self.assertEqual(parse_line("1,5 kg pommes de terre")["quantity"], D("1.500"))
        self.assertEqual(parse_line("3 tomates"),
                         {"name": "tomates", "quantity": D("3.000"), "unit": ""})
        self.assertEqual(parse_line("1/2 citron")["quantity"], D("0.500"))
        self.assertEqual(parse_line("2 Litres d'eau"),
                         {"name": "eau", "quantity": D("2.000"), "unit": "l"})
        self.assertEqual(parse_line("2 boîtes de tomates")["name"], "boîtes de tomates")
        self.assertEqual(parse_line("sel"), {"name": "sel", "quantity": None, "unit": ""})
        self.assertEqual(parse_line("3 gorgonzola")["unit"], "")

    def test_invalid_lines(self):
        for bad in ["0 g riz", "2 l", "12", "1/0 riz", "5000000 g riz", "0,0001 g riz", "x" * 121]:
            with self.assertRaises(ValueError, msg=bad):
                parse_line(bad)

    def test_round_trip(self):
        for text in ["500 g riz", "3 tomates", "sel", "1,5 kg pommes de terre", "250 ml crème"]:
            item = parse_line(text)
            again = parse_line(format_line(item["name"], item["quantity"], item["unit"]))
            self.assertEqual(item, again, text)

    def test_aggregate(self):
        rows = [
            ("a", "tomates", D(3), ""), ("b", "Tomate", D(2), ""),
            ("a", "riz", D(500), "g"), ("b", "Riz", D(1), "kg"),
            ("a", "lait", D(250), "ml"), ("b", "Lait", D(1), "l"),
            ("a", "sel", None, ""), ("b", "sel", None, ""),
            ("a", "farine", D(500), "g"), ("b", "farine", D(2), ""),
            ("a", "Crème", D(10), "cl"), ("b", "creme", D(20), "cl"),
            ("a", "œufs", D(6), ""), ("b", "oeuf", D(2), ""),
        ]
        result = aggregate(rows)
        lines = [r["line"] for r in result]
        for expected in ("5 tomates", "1,5 kg riz", "1,25 l lait", "sel", "500 g farine",
                         "2 farine", "300 ml Crème", "8 œufs"):
            self.assertIn(expected, lines)
        self.assertEqual(next(r for r in result if r["name"] == "sel")["meals"], ["a", "b"])

    def test_fractions_add_up(self):
        rows = [("a", "citron", D("0.5"), ""), ("b", "citron", D("0.5"), ""),
                ("c", "citron", D("1"), "")]
        self.assertEqual(aggregate(rows)[0]["line"], "2 citron")


class MealViewTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.org = U.objects.create_user("orga", password="pw-test-123")
        self.other = U.objects.create_user("autre", password="pw-test-123")
        self.stranger = U.objects.create_user("intrus", password="pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        self.m_org = Member.objects.create(trip=self.trip, display_name="Orga",
                                           user=self.org, is_organizer=True)
        self.m_other = Member.objects.create(trip=self.trip, display_name="Autre",
                                             user=self.other)

    def make_meal(self, day, title, items, meal_type=2):
        meal = Meal.objects.create(trip=self.trip, title=title, day=day,
                                   meal_type=meal_type, proposed_by=self.m_org)
        for n, (name, qty, unit) in enumerate(items):
            MealItem.objects.create(meal=meal, name=name, quantity=qty, unit=unit, position=n)
        return meal

    def test_add_meal_parses_items(self):
        self.client.force_login(self.org)
        r = self.client.post(reverse("meal_add", args=[self.trip.pk]), {
            "title": "Pâtes", "day": 1, "meal_type": 4, "recipe": "",
            "items_text": "500 g de pâtes\n3 tomates\nsel"})
        self.assertEqual(r.status_code, 302)
        items = [(i.name, i.quantity, i.unit) for i in Meal.objects.get().items.all()]
        self.assertEqual(items, [("pâtes", D("500.000"), "g"),
                                 ("tomates", D("3.000"), ""), ("sel", None, "")])

    def test_invalid_item_line_is_reported(self):
        self.client.force_login(self.org)
        r = self.client.post(reverse("meal_add", args=[self.trip.pk]), {
            "title": "Pâtes", "day": 1, "meal_type": 4, "items_text": "sel\n0 g riz"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Ligne 2")
        self.assertEqual(Meal.objects.count(), 0)

    def test_edit_shows_existing_items_and_replaces_them(self):
        meal = self.make_meal(1, "Riz", [("riz", D("500"), "g")])
        self.client.force_login(self.org)
        url = reverse("meal_edit", args=[meal.pk])
        self.assertContains(self.client.get(url), "500 g riz")
        self.client.post(url, {"title": "Riz", "day": 1, "meal_type": 2,
                               "items_text": "2 kg riz"})
        self.assertEqual([i.display for i in meal.items.all()], ["2 kg riz"])

    def test_shopping_list_adds_quantities_over_a_day_range(self):
        self.make_meal(1, "Riz", [("riz", D("500"), "g"), ("tomates", D("2"), "")])
        self.make_meal(2, "Salade", [("riz", D("1"), "kg"), ("sel", None, "")])
        self.make_meal(3, "Tomates farcies", [("tomates", D("3"), "")])
        self.client.force_login(self.other)
        url = reverse("meal_shopping", args=[self.trip.pk])
        r = self.client.get(url, {"from": "1", "to": "2"})
        self.assertContains(r, "1,5 kg riz")
        self.assertContains(r, "2 tomates")
        self.assertNotContains(r, "5 tomates")
        self.assertContains(self.client.get(url), "5 tomates")

    def test_checked_meals_take_priority_over_range(self):
        first = self.make_meal(1, "Riz", [("riz", D("500"), "g")])
        self.make_meal(2, "Pâtes", [("pâtes", D("300"), "g")])
        self.client.force_login(self.org)
        r = self.client.get(reverse("meal_shopping", args=[self.trip.pk]),
                            {"m": str(first.pk), "from": "2", "to": "2"})
        self.assertContains(r, "500 g riz")
        self.assertNotContains(r, "pâtes")

    def test_odd_query_values_do_not_crash(self):
        self.client.force_login(self.org)
        r = self.client.get(reverse("meal_shopping", args=[self.trip.pk]),
                            {"from": "²", "to": "x", "m": "٣"})
        self.assertEqual(r.status_code, 200)

    def test_only_proposer_or_organizer_can_edit_or_delete(self):
        meal = self.make_meal(1, "Riz", [])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("meal_edit", args=[meal.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("meal_delete", args=[meal.pk])).status_code, 403)
        self.assertTrue(Meal.objects.filter(pk=meal.pk).exists())
        self.client.force_login(self.org)
        self.client.post(reverse("meal_delete", args=[meal.pk]))
        self.assertFalse(Meal.objects.filter(pk=meal.pk).exists())

    def test_outsider_gets_404(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(reverse("meals", args=[self.trip.pk])).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("meal_shopping", args=[self.trip.pk])).status_code, 404)

class RoleTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.users = {n: U.objects.create_user(n, password="pw-test-123")
                      for n in ("orga", "coorga", "chef", "simple", "stranger")}
        self.trip = Trip.objects.create(name="Test")

        def make(name, **kw):
            return Member.objects.create(trip=self.trip, display_name=name.title(),
                                         user=self.users[name], **kw)

        self.orga = make("orga", is_organizer=True)
        self.coorga = make("coorga", roles=["coorganizer"])
        self.chef = make("chef", roles=["chef"])
        self.simple = make("simple")
        self.url = reverse("roles", args=[self.trip.pk])

    def post_roles(self, as_user, member, roles):
        self.client.force_login(self.users[as_user])
        return self.client.post(self.url, {f"roles_{member.pk}": roles})

    def test_capabilities(self):
        by_simple = Meal(proposed_by_id=self.simple.pk)
        by_orga = Meal(proposed_by_id=self.orga.pk)
        self.assertTrue(self.orga.can_manage and self.coorga.can_manage)
        self.assertFalse(self.chef.can_manage or self.simple.can_manage)
        self.assertTrue(self.chef.can_edit_meal(by_orga))
        self.assertTrue(self.coorga.can_edit_meal(by_orga))
        self.assertTrue(self.simple.can_edit_meal(by_simple))
        self.assertFalse(self.simple.can_edit_meal(by_orga))

    def test_assignable_roles(self):
        every = {"coorganizer", "chef", "accountant", "agent"}
        self.assertEqual(self.orga.assignable_roles(), every)
        self.assertEqual(self.coorga.assignable_roles(), every - {"coorganizer"})
        self.assertEqual(self.chef.assignable_roles(), set())
        self.assertEqual(self.simple.assignable_roles(), set())

    def test_organizer_can_assign_both_roles(self):
        self.post_roles("orga", self.simple, ["coorganizer", "chef"])
        self.simple.refresh_from_db()
        self.assertEqual(sorted(self.simple.roles), ["chef", "coorganizer"])

    def test_coorganizer_can_assign_chef_but_not_coorganizer(self):
        self.post_roles("coorga", self.simple, ["coorganizer", "chef"])
        self.simple.refresh_from_db()
        self.assertEqual(self.simple.roles, ["chef"])

    def test_coorganizer_cannot_remove_coorganizer_role_but_can_remove_chef(self):
        self.client.force_login(self.users["coorga"])
        self.client.post(self.url, {})
        self.coorga.refresh_from_db()
        self.chef.refresh_from_db()
        self.assertEqual(self.coorga.roles, ["coorganizer"])
        self.assertEqual(self.chef.roles, [])

    def test_organizer_role_cannot_be_changed(self):
        self.post_roles("orga", self.orga, ["chef"])
        self.orga.refresh_from_db()
        self.assertEqual(self.orga.roles, [])

    def test_chef_and_simple_member_cannot_open_roles_page(self):
        for name in ("chef", "simple"):
            self.client.force_login(self.users[name])
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.post(self.url, {}).status_code, 403)

    def test_stranger_gets_404(self):
        self.client.force_login(self.users["stranger"])
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_chef_can_edit_and_delete_any_meal_but_simple_member_cannot(self):
        meal = Meal.objects.create(trip=self.trip, title="Raclette", day=1,
                                   proposed_by=self.orga)
        edit = reverse("meal_edit", args=[meal.pk])
        self.client.force_login(self.users["simple"])
        self.assertEqual(self.client.get(edit).status_code, 403)
        self.client.force_login(self.users["chef"])
        self.assertEqual(self.client.get(edit).status_code, 200)
        self.client.post(reverse("meal_delete", args=[meal.pk]))
        self.assertFalse(Meal.objects.filter(pk=meal.pk).exists())

    def test_coorganizer_has_organizer_powers_but_chef_does_not(self):
        def make_expense():
            return Expense.objects.create(trip=self.trip, paid_by=self.simple,
                                          created_by=self.simple, label="x",
                                          amount=Decimal("10"), split_mode="custom")
        expense = make_expense()
        self.client.force_login(self.users["chef"])
        self.assertEqual(self.client.post(reverse("expense_delete", args=[expense.pk])).status_code, 403)
        self.client.force_login(self.users["coorga"])
        self.client.post(reverse("expense_delete", args=[expense.pk]))
        self.assertFalse(Expense.objects.filter(pk=expense.pk).exists())


class LegIdentityTests(SimpleTestCase):
    def test_normalize_place(self):
        for a, b in [("Chambéry", "  chambery "), ("Saint-Étienne", "SAINT ETIENNE"),
                     ("Paris – Gare de Lyon", "paris gare de lyon"), ("Grenoble", "GRENOBLE.")]:
            self.assertEqual(normalize_place(a), normalize_place(b))
        self.assertNotEqual(normalize_place("Paris"), normalize_place("Pau"))
        self.assertEqual(normalize_place(None), "")

    def test_leg_key(self):
        t = djtz.make_aware(datetime(2027, 2, 6, 8, 30))
        base = ("out", "train", "Paris", t, "Grenoble", t + timedelta(hours=3))
        same = ("out", "train", " paris ", t, "GRENOBLE", t + timedelta(hours=3))
        self.assertEqual(leg_key(*base), leg_key(*same))
        for changed in [("back",) + base[1:], (base[0], "bus") + base[2:],
                        base[:3] + (t + timedelta(minutes=1),) + base[4:],
                        base[:4] + ("Lyon", base[5]), base[:5] + (None,)]:
            self.assertNotEqual(leg_key(*base), leg_key(*changed))

    def test_sort_key(self):
        t = djtz.make_aware(datetime(2027, 2, 6, 8, 30))
        legs = [SimpleNamespace(pk=3, departure_at=None),
                SimpleNamespace(pk=1, departure_at=t + timedelta(hours=5)),
                SimpleNamespace(pk=2, departure_at=t),
                SimpleNamespace(pk=4, departure_at=None)]
        self.assertEqual([l.pk for l in sorted(legs, key=sort_key)], [2, 1, 3, 4])


class LegTests(TestCase):
    def setUp(self):
        U = get_user_model()
        names = ("orga", "dora", "alice", "bob", "carl", "stranger")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test")
        self.m = {}
        for n in names[:-1]:
            self.m[n] = Member.objects.create(trip=self.trip, display_name=n.title(),
                                              user=self.users[n], is_organizer=(n == "orga"))
        self.depart = djtz.make_aware(datetime(2027, 2, 6, 8, 30))
        self.arrive = djtz.make_aware(datetime(2027, 2, 6, 11, 45))

    def as_user(self, name):
        self.client.force_login(self.users[name])

    def train(self, price=None, **kw):
        data = dict(trip=self.trip, name="Train Paris–Grenoble", transport="train",
                    departure_place="Paris", arrival_place="Grenoble", direction="out",
                    departure_at=self.depart, arrival_at=self.arrive,
                    proposed_by=self.m["dora"])
        data.update(kw)
        leg = Leg.objects.create(**data)
        LegParticipant.objects.create(leg=leg, member=self.m["dora"], price=price)
        return leg

    def car(self, seats=2, **kw):
        data = dict(trip=self.trip, name="Voiture", transport="car", seats=seats,
                    departure_place="Paris", arrival_place="Grenoble", direction="out",
                    proposed_by=self.m["dora"])
        data.update(kw)
        return Leg.objects.create(**data)

    def payload(self, **kw):
        data = {"direction": "out", "name": "Train", "transport": "train",
                "departure_place": "Paris", "departure_at": "2027-02-06T08:30",
                "arrival_place": "Grenoble", "arrival_at": "2027-02-06T11:45",
                "status": "planned"}
        data.update(kw)
        return data

    def add_url(self):
        return reverse("leg_add", args=[self.trip.pk])

    def prices(self, leg):
        return {p.member_id: p.price for p in leg.participations.all()}

    # --- création, trajets identiques, « Prendre ce trajet » -----------------------------

    def test_creating_a_train_makes_the_creator_a_participant_with_their_price(self):
        self.as_user("dora")
        r = self.client.post(self.add_url(), self.payload(price="45.50"))
        self.assertEqual(r.status_code, 302)
        leg = Leg.objects.get()
        self.assertIsNone(leg.price)
        self.assertEqual(self.prices(leg), {self.m["dora"].pk: Decimal("45.50")})
        page = self.client.get(reverse("legs", args=[self.trip.pk]))
        self.assertContains(page, "Dora")
        self.assertContains(page, "45,50")
        self.assertNotContains(page, "Proposé par")

    def test_identical_trip_adds_the_member_without_creating_a_new_leg(self):
        leg = self.train(price=Decimal("40"))
        self.as_user("alice")
        r = self.client.post(self.add_url(),
                             self.payload(name="TGV 6601", status="confirmed", price="38"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Leg.objects.count(), 1)
        leg.refresh_from_db()
        self.assertEqual((leg.name, leg.status), ("Train Paris–Grenoble", "planned"))
        self.assertEqual(self.prices(leg), {self.m["dora"].pk: Decimal("40"),
                                            self.m["alice"].pk: Decimal("38")})

    def test_places_are_compared_ignoring_case_accents_and_spacing(self):
        leg = self.train(departure_place="Chambéry", arrival_place="Saint-Étienne")
        self.as_user("alice")
        self.client.post(self.add_url(), self.payload(departure_place="  chambery ",
                                                      arrival_place="SAINT ETIENNE"))
        self.assertEqual(Leg.objects.count(), 1)
        self.assertEqual(leg.participations.count(), 2)

    def test_a_different_time_direction_or_transport_creates_a_new_leg(self):
        self.train()
        self.as_user("alice")
        self.client.post(self.add_url(), self.payload(departure_at="2027-02-06T09:30"))
        self.client.post(self.add_url(), self.payload(direction="back"))
        self.client.post(self.add_url(), self.payload(transport="shuttle"))
        self.assertEqual(Leg.objects.count(), 4)

    def test_identical_cars_are_never_merged(self):
        self.car()
        self.as_user("alice")
        self.client.post(self.add_url(), self.payload(
            name="Ma voiture", transport="car", seats=3, price="8"))
        self.assertEqual(Leg.objects.count(), 2)
        mine = Leg.objects.get(name="Ma voiture")
        self.assertEqual(mine.price, Decimal("8"))
        self.assertEqual(mine.participations.count(), 0)

    def test_taking_twice_only_updates_the_price(self):
        leg = self.train()
        self.as_user("alice")
        self.client.post(self.add_url(), self.payload(price="10"))
        self.client.post(self.add_url(), self.payload(price="12"))
        self.assertEqual(leg.participations.count(), 2)
        self.assertEqual(self.prices(leg)[self.m["alice"].pk], Decimal("12"))

    def test_take_this_trip_button_and_prefilled_form(self):
        leg = self.train()
        self.as_user("alice")
        page = self.client.get(reverse("legs", args=[self.trip.pk]))
        self.assertContains(page, "Prendre ce trajet")
        self.assertContains(page, f"?from={leg.pk}")
        form = self.client.get(self.add_url(), {"from": leg.pk})
        for expected in ('value="Paris"', 'value="Grenoble"', 'value="2027-02-06T08:30"'):
            self.assertContains(form, expected)
        self.assertNotContains(form, 'name="price" value')

    def test_participants_do_not_see_the_take_button(self):
        self.train()
        self.as_user("dora")
        page = self.client.get(reverse("legs", args=[self.trip.pk]))
        self.assertNotContains(page, "?from=")
        self.assertContains(page, "Me retirer")

    def test_prefill_ignores_cars_foreign_legs_and_unknown_ids(self):
        car = self.car()
        other = Trip.objects.create(name="Autre")
        foreign = Leg.objects.create(trip=other, name="Étranger", transport="train",
                                     departure_place="Rome", arrival_place="Milan",
                                     direction="out")
        self.as_user("alice")
        for pk in (car.pk, foreign.pk, 99999):
            r = self.client.get(self.add_url(), {"from": pk})
            self.assertNotContains(r, 'value="Rome"')
            self.assertNotContains(r, 'value="Paris"')

    # --- prix personnel, départ, retrait ---------------------------------------------------

    def test_participant_can_set_and_clear_their_own_price(self):
        leg = self.train()
        self.as_user("dora")
        url = reverse("leg_price", args=[leg.pk])
        self.client.post(url, {"price": "42,5"})
        self.assertEqual(leg.participations.get().price, Decimal("42.50"))
        for bad in ("abc", "-3", "NaN", "Infinity", "1e999", "9999999"):
            self.client.post(url, {"price": bad})
            self.assertEqual(leg.participations.get().price, Decimal("42.50"), bad)
        self.client.post(url, {"price": ""})
        self.assertIsNone(leg.participations.get().price)

    def test_non_participant_cannot_set_a_price(self):
        leg = self.train()
        self.as_user("alice")
        self.client.post(reverse("leg_price", args=[leg.pk]), {"price": "10"})
        self.assertEqual(leg.participations.count(), 1)

    def test_leaving_and_empty_legs_are_deleted(self):
        leg = self.train()
        LegParticipant.objects.create(leg=leg, member=self.m["alice"])
        self.as_user("alice")
        self.client.post(reverse("leg_leave", args=[leg.pk]))
        self.assertEqual(leg.participations.count(), 1)
        self.as_user("dora")
        self.client.post(reverse("leg_leave", args=[leg.pk]))
        self.assertEqual(Leg.objects.count(), 0)

    def test_a_non_participant_cannot_delete_an_empty_leg_by_leaving(self):
        leg = Leg.objects.create(trip=self.trip, name="Orphelin", transport="train",
                                 departure_place="A", arrival_place="B", direction="out")
        self.as_user("alice")
        self.client.post(reverse("leg_leave", args=[leg.pk]))
        self.assertTrue(Leg.objects.filter(pk=leg.pk).exists())

    def test_remove_participant_permissions(self):
        leg = self.train()
        for n in ("alice", "bob", "carl"):
            LegParticipant.objects.create(leg=leg, member=self.m[n])
        url = reverse("leg_remove", args=[leg.pk])
        self.as_user("bob")
        self.assertEqual(self.client.post(url, {"member_id": self.m["alice"].pk}).status_code, 403)
        self.as_user("dora")
        self.client.post(url, {"member_id": self.m["alice"].pk})
        self.as_user("orga")
        self.client.post(url, {"member_id": self.m["bob"].pk})
        self.assertEqual({p.member_id for p in leg.participations.all()},
                         {self.m["dora"].pk, self.m["carl"].pk})

    # --- modification --------------------------------------------------------------------

    def test_cannot_switch_between_car_and_other_transports_but_train_to_shuttle_is_fine(self):
        car, train = self.car(), self.train()
        self.as_user("dora")
        r = self.client.post(reverse("leg_edit", args=[car.pk]),
                             self.payload(name="Voiture", transport="train"))
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("leg_edit", args=[train.pk]),
                             self.payload(transport="car", seats=3))
        self.assertEqual(r.status_code, 200)
        car.refresh_from_db()
        train.refresh_from_db()
        self.assertEqual((car.transport, train.transport), ("car", "train"))
        r = self.client.post(reverse("leg_edit", args=[train.pk]),
                             self.payload(transport="shuttle"))
        self.assertEqual(r.status_code, 302)
        train.refresh_from_db()
        self.assertEqual(train.transport, "shuttle")

    def test_editing_into_an_identical_leg_is_rejected(self):
        self.train()
        later = self.train(name="Plus tard", departure_at=self.depart + timedelta(hours=2),
                           arrival_at=self.arrive + timedelta(hours=2))
        self.as_user("dora")
        r = self.client.post(reverse("leg_edit", args=[later.pk]), self.payload(name="Plus tard"))
        self.assertEqual(r.status_code, 200)
        later.refresh_from_db()
        self.assertEqual(later.departure_at, self.depart + timedelta(hours=2))

    def test_price_field_only_when_editing_cars(self):
        train, car = self.train(), self.car()
        self.as_user("dora")
        self.assertNotContains(self.client.get(reverse("leg_edit", args=[train.pk])), 'name="price"')
        self.assertContains(self.client.get(reverse("leg_edit", args=[car.pk])), 'name="price"')

    def test_only_proposer_or_manager_can_edit_and_delete(self):
        leg = self.train()
        self.as_user("alice")
        self.assertEqual(self.client.get(reverse("leg_edit", args=[leg.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("leg_delete", args=[leg.pk])).status_code, 403)
        self.as_user("orga")
        self.client.post(reverse("leg_delete", args=[leg.pk]))
        self.assertEqual(Leg.objects.count(), 0)

    # --- affichage ------------------------------------------------------------------------

    def test_legs_are_listed_by_departure_time_with_unknown_times_last(self):
        self.train(departure_place="Briancon", departure_at=self.depart + timedelta(hours=5),
                   arrival_at=None)
        self.train(departure_place="Chamonix", departure_at=None, arrival_at=None)
        self.train(departure_place="Annecy")
        self.as_user("alice")
        html = self.client.get(reverse("legs", args=[self.trip.pk])).content.decode()
        self.assertLess(html.index("Annecy"), html.index("Briancon"))
        self.assertLess(html.index("Briancon"), html.index("Chamonix"))

#    def test_legs_are_listed_by_departure_time_with_unknown_times_last(self):
#        self.train(name="Second", departure_at=self.depart + timedelta(hours=5), arrival_at=None)
#        self.train(name="Sans heure", departure_at=None, arrival_at=None,
#                   departure_place="Lyon")
#        self.train(name="Premier")
#        self.as_user("alice")
#        html = self.client.get(reverse("legs", args=[self.trip.pk])).content.decode()
#        self.assertLess(html.index("Premier"), html.index("Second"))
#        self.assertLess(html.index("Second"), html.index("Sans heure"))

    def test_cars_show_the_driver_and_other_transports_show_participants(self):
        self.train()
        self.car()
        self.as_user("alice")
        page = self.client.get(reverse("legs", args=[self.trip.pk]))
        self.assertContains(page, "Conducteur : Dora")
        self.assertContains(page, "Participants")

    # --- voitures (inchangé) -----------------------------------------------------------------

    def test_car_requires_seats(self):
        self.as_user("dora")
        r = self.client.post(self.add_url(), self.payload(transport="car"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Leg.objects.count(), 0)

    def test_non_car_ignores_seats(self):
        self.as_user("dora")
        self.client.post(self.add_url(), self.payload(seats=3))
        self.assertIsNone(Leg.objects.get().seats)

    def test_arrival_before_departure_is_rejected(self):
        self.as_user("dora")
        r = self.client.post(self.add_url(), self.payload(
            departure_at="2027-02-06T10:00", arrival_at="2027-02-06T08:00"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Leg.objects.count(), 0)

    def test_join_respects_capacity(self):
        leg = self.car(seats=2)
        for name in ("alice", "bob", "carl"):
            self.as_user(name)
            self.client.post(reverse("leg_join", args=[leg.pk]))
        self.assertEqual(leg.passengers.count(), 2)
        self.assertFalse(leg.passengers.filter(pk=self.m["carl"].pk).exists())

    def test_driver_cannot_join_own_car_and_nobody_joins_a_train_as_passenger(self):
        car, train = self.car(), self.train()
        self.as_user("dora")
        self.client.post(reverse("leg_join", args=[car.pk]))
        self.as_user("alice")
        self.client.post(reverse("leg_join", args=[train.pk]))
        self.assertEqual(car.passengers.count(), 0)
        self.assertEqual(train.passengers.count(), 0)

    def test_passenger_can_leave_a_car(self):
        leg = self.car()
        leg.passengers.add(self.m["alice"])
        self.as_user("alice")
        self.client.post(reverse("leg_leave", args=[leg.pk]))
        self.assertEqual(leg.passengers.count(), 0)
        self.assertTrue(Leg.objects.filter(pk=leg.pk).exists())

    def test_remove_passenger_permissions(self):
        leg = self.car(seats=3)
        leg.passengers.add(self.m["alice"], self.m["bob"], self.m["carl"])
        url = reverse("leg_remove", args=[leg.pk])
        self.as_user("bob")
        self.assertEqual(self.client.post(url, {"member_id": self.m["alice"].pk}).status_code, 403)
        self.as_user("dora")
        self.client.post(url, {"member_id": self.m["alice"].pk})
        self.as_user("orga")
        self.client.post(url, {"member_id": self.m["bob"].pk})
        self.assertEqual([p.pk for p in leg.passengers.all()], [self.m["carl"].pk])

    def test_seats_cannot_drop_below_passengers(self):
        leg = self.car(seats=2)
        leg.passengers.add(self.m["alice"], self.m["bob"])
        self.as_user("dora")
        r = self.client.post(reverse("leg_edit", args=[leg.pk]), self.payload(
            name="Voiture", transport="car", seats=1))
        self.assertEqual(r.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.seats, 2)

    def test_outsider_gets_404(self):
        leg, car = self.train(), self.car()
        self.as_user("stranger")
        self.assertEqual(self.client.get(reverse("legs", args=[self.trip.pk])).status_code, 404)
        self.assertEqual(self.client.get(f"{self.add_url()}?from={leg.pk}").status_code, 404)
        for name, target in (("leg_join", car), ("leg_leave", leg), ("leg_price", leg)):
            self.assertEqual(self.client.post(reverse(name, args=[target.pk])).status_code, 404, name)



class HiddenExpenseTests(TestCase):
    def setUp(self):
        U = get_user_model()
        names = ("orga", "bob", "carl", "dana", "stranger")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test")
        self.m = {}
        for n in names[:-1]:
            self.m[n] = Member.objects.create(
                trip=self.trip, display_name=n.title(), user=self.users[n],
                is_organizer=(n == "orga"), roles=["coorganizer"] if n == "dana" else [])
        # visible : carl paie 40 €, 10 € chacun
        self.visible = self.expense("Courses", "40", "carl",
                                    {"orga": "10", "bob": "10", "carl": "10", "dana": "10"})
        # cadeau caché à orga (organisateur) : bob paie 87 €, 29 € chacun pour bob, carl, dana
        self.gift = self.expense("Cadeau secret", "87", "bob",
                                 {"bob": "29", "carl": "29", "dana": "29"}, hidden=["orga"])

    def expense(self, label, amount, payer, shares, hidden=()):
        e = Expense.objects.create(trip=self.trip, paid_by=self.m[payer],
                                   created_by=self.m[payer], label=label,
                                   amount=Decimal(amount), split_mode="custom")
        for name, value in shares.items():
            ExpenseShare.objects.create(expense=e, member=self.m[name], amount=Decimal(value))
        e.hidden_from.set([self.m[h] for h in hidden])
        return e

    def budget_page(self, name):
        self.client.force_login(self.users[name])
        return self.client.get(reverse("budget", args=[self.trip.pk]))

    def all_pks(self):
        return [m.pk for m in self.m.values()]

    def test_recipient_sees_nothing_even_as_organizer(self):
        r = self.budget_page("orga")
        self.assertContains(r, "40,00")
        for text in ("Cadeau secret", "cachées à", "87,00", "29,00", "58,00"):
            self.assertNotContains(r, text)

    def test_recipient_with_coorganizer_role_sees_nothing(self):
        self.expense("Surprise pour Dana", "63", "bob",
                     {"bob": "21", "carl": "21", "orga": "21"}, hidden=["dana"])
        r = self.budget_page("dana")
        for text in ("Surprise pour Dana", "63,00", "21,00", "42,00"):
            self.assertNotContains(r, text)
        self.assertContains(r, "Cadeau secret")

    def test_non_recipients_see_the_hidden_section(self):
        r = self.budget_page("carl")
        self.assertContains(r, "Cadeau secret")
        self.assertContains(r, "cachées à Orga")
        self.assertContains(r, "87,00")

    def test_layers_are_separate(self):
        b = self.m
        _, layers = trip_calc(self.trip)
        visible, hidden = layers
        self.assertEqual(visible["balances"], {b["orga"].pk: -1000, b["bob"].pk: -1000,
                                               b["carl"].pk: 3000, b["dana"].pk: -1000})
        self.assertEqual(hidden["balances"][b["bob"].pk], 5800)
        self.assertEqual(hidden["balances"][b["orga"].pk], 0)
        self.assertEqual(hidden["transfers"], [(b["carl"].pk, b["bob"].pk, 2900),
                                               (b["dana"].pk, b["bob"].pk, 2900)])
        self.assertEqual(trip_balances(self.trip)[1], visible["balances"])

    def test_recipient_cannot_open_edit_or_delete_a_hidden_expense(self):
        self.client.force_login(self.users["orga"])
        self.assertEqual(self.client.get(reverse("expense_edit", args=[self.gift.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("expense_delete", args=[self.gift.pk])).status_code, 404)
        self.assertTrue(Expense.objects.filter(pk=self.gift.pk).exists())

    def test_recipient_cannot_settle_the_hidden_layer(self):
        b = self.m
        self.client.force_login(self.users["orga"])
        r = self.client.post(reverse("settle", args=[self.trip.pk]), {
            "from_id": b["carl"].pk, "to_id": b["bob"].pk, "amount_cents": 2900,
            "layer": str(b["orga"].pk)})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Expense.objects.filter(is_transfer=True).exists())

    def test_hidden_transfer_stays_hidden_and_does_not_touch_the_visible_layer(self):
        b = self.m
        self.client.force_login(self.users["carl"])
        data = {"from_id": b["carl"].pk, "to_id": b["bob"].pk, "amount_cents": 2900}
        self.assertEqual(self.client.post(reverse("settle", args=[self.trip.pk]),
                                          {**data, "layer": ""}).status_code, 404)
        r = self.client.post(reverse("settle", args=[self.trip.pk]),
                             {**data, "layer": str(b["orga"].pk)})
        self.assertEqual(r.status_code, 302)
        transfer = Expense.objects.get(is_transfer=True)
        self.assertEqual([h.pk for h in transfer.hidden_from.all()], [b["orga"].pk])
        _, layers = trip_calc(self.trip)
        self.assertEqual(layers[1]["balances"][b["carl"].pk], 0)
        self.assertEqual(layers[0]["balances"][b["carl"].pk], 3000)
        self.assertNotContains(self.budget_page("orga"), "<b>Remboursement</b>")
        self.assertContains(self.budget_page("carl"), "<b>Remboursement</b>")

    def post_expense(self, as_user, **overrides):
        self.client.force_login(self.users[as_user])
        data = {"label": "Fleurs", "amount": "30.00", "category": "autre",
                "date": "2027-02-06", "paid_by": self.m[as_user].pk, "split_mode": "equal",
                "participants": self.all_pks(), "hidden_from": [self.m["orga"].pk]}
        data.update(overrides)
        return self.client.post(reverse("expense_add", args=[self.trip.pk]), data)

    def test_creating_a_hidden_expense_excludes_the_recipient_automatically(self):
        b = self.m
        self.assertEqual(self.post_expense("bob").status_code, 302)
        e = Expense.objects.get(label="Fleurs")
        self.assertEqual([h.pk for h in e.hidden_from.all()], [b["orga"].pk])
        self.assertEqual(sorted(s.member_id for s in e.shares.all()),
                         sorted([b["bob"].pk, b["carl"].pk, b["dana"].pk]))

    def test_recipient_cannot_be_the_payer(self):
        r = self.post_expense("bob", paid_by=self.m["orga"].pk)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Expense.objects.filter(label="Fleurs").exists())

    def test_recipient_cannot_have_a_custom_share(self):
        r = self.post_expense("bob", split_mode="custom", **{
            f"share_{self.m['orga'].pk}": "10", f"share_{self.m['bob'].pk}": "20"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Expense.objects.filter(label="Fleurs").exists())

    def test_creator_cannot_hide_an_expense_from_themselves(self):
        r = self.post_expense("bob", hidden_from=[self.m["bob"].pk])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Expense.objects.filter(label="Fleurs").exists())

    def test_audience_cannot_be_changed_by_editing(self):
        b = self.m
        self.client.force_login(self.users["bob"])
        r = self.client.post(reverse("expense_edit", args=[self.gift.pk]), {
            "label": "Cadeau secret", "amount": "87.00", "category": "autre",
            "date": "2027-02-06", "paid_by": b["bob"].pk, "split_mode": "equal",
            "participants": self.all_pks(), "hidden_from": []})
        self.assertEqual(r.status_code, 302)
        self.gift.refresh_from_db()
        self.assertEqual([h.pk for h in self.gift.hidden_from.all()], [b["orga"].pk])
        self.assertNotIn(b["orga"].pk, [s.member_id for s in self.gift.shares.all()])

    def test_only_creator_or_manager_among_non_recipients_can_edit(self):
        url = reverse("expense_edit", args=[self.gift.pk])
        self.client.force_login(self.users["carl"])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.users["dana"])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_outsider_gets_404(self):
        self.client.force_login(self.users["stranger"])
        self.assertEqual(self.client.get(reverse("budget", args=[self.trip.pk])).status_code, 404)

class PeriodsParsingTests(SimpleTestCase):
    TODAY = date(2026, 11, 20)

    def test_valid_file(self):
        periods, errors = parse_periods(
            "début;fin\n06/02/2027;13/02/2027\n13/02/2027;20/02/2027\n", self.TODAY)
        self.assertEqual(periods, [(date(2027, 2, 6), date(2027, 2, 13)),
                                   (date(2027, 2, 13), date(2027, 2, 20))])
        self.assertEqual(errors, [])

    def test_variants_are_accepted(self):
        periods, _ = parse_periods("Début,Fin\r\n6/2/2027,13/2/2027\r\n"
                                   "2027-03-01,2027-03-05\r\n", self.TODAY)
        self.assertEqual(periods, [(date(2027, 2, 6), date(2027, 2, 13)),
                                   (date(2027, 3, 1), date(2027, 3, 5))])
        self.assertEqual(len(parse_periods("\ufeffdébut;fin\n06/02/2027;13/02/2027",
                                           self.TODAY)[0]), 1)

    def test_errors_duplicates_and_blank_lines(self):
        text = ("début;fin\n06/02/2027;13/02/2027\n\n06/02/2027;13/02/2027\n"
                "13/02/2027;06/02/2027\n01/01/2020;05/01/2020\nabc;13/02/2027\n06/02/2027\n"
                "01/02/2027;15/05/2027\n01/01/2031;05/01/2031\n")
        periods, errors = parse_periods(text, self.TODAY)
        self.assertEqual(periods, [(date(2027, 2, 6), date(2027, 2, 13))])
        self.assertEqual(len(errors), 6)
        self.assertTrue(errors[0].startswith("Ligne 5 :"))
        self.assertIn("Ligne 8 : colonnes manquantes.", errors)

    def test_bad_files_are_rejected(self):
        for bad in ["", "   \n", "a;b\n1;2", "nom;gare\nx;y"]:
            with self.assertRaises(ValueError):
                parse_periods(bad, self.TODAY)
        with self.assertRaises(ValueError):
            parse_periods("début;fin\n" + "06/02/2027;13/02/2027\n" * 101, self.TODAY)


class RankingTests(SimpleTestCase):
    def test_top_pairs(self):
        dv = [(1, 10, 2), (2, 10, 2), (3, 10, 1),
              (1, 20, 2), (2, 20, 2), (3, 20, 0)]
        tv = [(1, 100, 2), (2, 100, 0), (3, 100, 2),
              (1, 200, 2), (2, 200, 2), (3, 200, 1)]
        stats = summarize([10, 20], dv)
        self.assertEqual(stats[10]["total"], 5)
        self.assertEqual(stats[20]["no_members"], {3})
        out = top_pairs(dv, tv, [10, 20], [100, 200], 3)
        self.assertEqual([(p["dest"], p["date"]) for p in out],
                         [(10, 200), (20, 200), (10, 100)])
        self.assertEqual(out[0]["score"], 5)
        self.assertEqual((out[1]["score"], out[1]["no"]), (4, 1))
        self.assertEqual((out[2]["score"], out[2]["no"]), (3, 1))

    def test_fewer_no_votes_win_ties(self):
        """Score combiné égal, mais la destination 1 a moins de Non combinés."""
        dv = [(1, 1, 1), (2, 1, 2), (3, 1, 1),
              (1, 2, 2), (2, 2, 0), (3, 2, 2)]
        tv = [(1, 9, 2), (2, 9, 2), (3, 9, 2)]
        out = top_pairs(dv, tv, [1, 2], [9], 2)
        self.assertEqual([p["dest"] for p in out], [1, 2])
        self.assertEqual(out[0]["score"], out[1]["score"])
        self.assertEqual((out[0]["no"], out[1]["no"]), (0, 1))

    def test_empty_inputs(self):
        self.assertEqual(top_pairs([], [], [], [], 3), [])
        self.assertEqual(summarize([1], [(5, 1, 2)])[1]["total"], 2)
        self.assertEqual(summarize([1], [(1, 9, 2)])[1]["total"], 0)  # élément inconnu ignoré


class PodiumViewTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.users = [U.objects.create_user(n, password="pw-test-123") for n in ("a", "b", "c")]
        self.trip = Trip.objects.create(name="Test")
        self.members = [Member.objects.create(trip=self.trip, display_name=n.title(), user=u)
                        for n, u in zip("abc", self.users)]
        self.alpes = Destination.objects.create(trip=self.trip, name="Alpes")
        self.bretagne = Destination.objects.create(trip=self.trip, name="Bretagne")
        self.mars = DateOption.objects.create(trip=self.trip, start_date=date(2027, 3, 1),
                                              end_date=date(2027, 3, 8))
        self.avril = DateOption.objects.create(trip=self.trip, start_date=date(2027, 4, 1),
                                               end_date=date(2027, 4, 8))
        self.url = reverse("trip_summary", args=[self.trip.pk])

    def test_no_votes_no_podium(self):
        self.client.force_login(self.users[0])
        self.assertContains(self.client.get(self.url), "Le classement apparaît")

    def test_podium_lists_best_pairs_first(self):
        for member, (a, b) in zip(self.members, [(2, 2), (2, 2), (1, 0)]):
            DestVote.objects.create(member=member, destination=self.alpes, score=a)
            DestVote.objects.create(member=member, destination=self.bretagne, score=b)
        for member, (m, v) in zip(self.members, [(2, 2), (0, 2), (2, 1)]):
            DateVote.objects.create(member=member, date_option=self.mars, score=m)
            DateVote.objects.create(member=member, date_option=self.avril, score=v)
        self.client.force_login(self.users[0])
        html = self.client.get(self.url).content.decode()
        podium = html.split('<ol class="podium">')[1].split("</ol>")[0]
        found = re.findall(r"<b>(.*?)</b>, du (.*?) au", podium)
        self.assertEqual(found, [("Alpes", "1 avril 2027"), ("Bretagne", "1 avril 2027"),
                                 ("Alpes", "1 mars 2027")])

    def test_pair_score_uses_min_per_voter(self):
        """Le Non de b sur Bretagne ne bloque pas Alpes pour b, mais écrase
        Bretagne à 0 pour lui, quel que soit son vote période."""
        for member in self.members:
            DestVote.objects.create(member=member, destination=self.alpes, score=2)
            DestVote.objects.create(member=member, destination=self.bretagne, score=2)
        for member, (m, v) in zip(self.members, [(2, 2), (0, 2), (2, 2)]):
            DateVote.objects.create(member=member, date_option=self.mars, score=m)
            DateVote.objects.create(member=member, date_option=self.avril, score=v)
        self.client.force_login(self.users[0])
        html = self.client.get(self.url).content.decode()
        podium = html.split('<ol class="podium">')[1].split("</ol>")[0]
        found = re.findall(r"<b>(.*?)</b>, du (.*?) au", podium)
        self.assertEqual(found, [("Alpes", "1 avril 2027"), ("Bretagne", "1 avril 2027"),
                                 ("Alpes", "1 mars 2027")])


class DateImportTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.user = U.objects.create_user("anna", password="pw-test-123")
        self.stranger = U.objects.create_user("zoe", password="pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        self.member = Member.objects.create(trip=self.trip, display_name="Anna", user=self.user)
        self.url = reverse("date_import", args=[self.trip.pk])
        base = djtz.localdate() + timedelta(days=30)
        self.d1, self.d2, self.d3 = base, base + timedelta(days=7), base + timedelta(days=14)

    @staticmethod
    def fmt(d):
        return d.strftime("%d/%m/%Y")

    def upload(self, text, encoding="utf-8", **extra):
        f = SimpleUploadedFile("dates.csv", text.encode(encoding), content_type="text/csv")
        return self.client.post(self.url, {"file": f}, **extra)

    def test_import_creates_periods(self):
        self.client.force_login(self.user)
        r = self.upload(f"début;fin\n{self.fmt(self.d1)};{self.fmt(self.d2)}\n"
                        f"{self.fmt(self.d2)};{self.fmt(self.d3)}\n")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(DateOption.objects.count(), 2)
        self.assertEqual(DateOption.objects.first().proposed_by, self.member)

    def test_bad_lines_and_duplicates_are_reported(self):
        DateOption.objects.create(trip=self.trip, start_date=self.d1, end_date=self.d2)
        self.client.force_login(self.user)
        r = self.upload(f"début;fin\n{self.fmt(self.d1)};{self.fmt(self.d2)}\n"
                        f"{self.fmt(self.d2)};{self.fmt(self.d3)}\nabc;def\n", follow=True)
        self.assertEqual(DateOption.objects.count(), 2)
        self.assertContains(r, "1 période(s) importée(s), 1 déjà présente(s)")
        self.assertContains(r, "Ligne 4")

    def test_bad_header_is_rejected(self):
        self.client.force_login(self.user)
        r = self.upload("nom;gare\nx;y\n")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "En-tête attendu")
        self.assertEqual(DateOption.objects.count(), 0)

    def test_limit_of_26_periods(self):
        base = self.d1
        for i in range(25):
            DateOption.objects.create(trip=self.trip, start_date=base + timedelta(days=i),
                                      end_date=base + timedelta(days=i + 1))
        self.client.force_login(self.user)
        far = self.d3 + timedelta(days=40)
        r = self.upload(f"début;fin\n{self.fmt(far)};{self.fmt(far + timedelta(days=2))}\n"
                        f"{self.fmt(far + timedelta(days=5))};{self.fmt(far + timedelta(days=6))}\n",
                        follow=True)
        self.assertEqual(DateOption.objects.count(), 26)
        self.assertContains(r, "non importée(s)")

    def test_windows_encoding(self):
        self.client.force_login(self.user)
        r = self.upload(f"début;fin\n{self.fmt(self.d1)};{self.fmt(self.d2)}\n", encoding="cp1252")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(DateOption.objects.count(), 1)

    def test_outsider_gets_404(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class LodgingTests(TestCase):
    def setUp(self):
        U = get_user_model()
        names = ("orga", "coorga", "agent", "owner", "simple", "stranger")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test")
        self.m = {}
        for n in names[:-1]:
            roles = {"coorga": ["coorganizer"], "agent": ["agent"]}.get(n, [])
            self.m[n] = Member.objects.create(trip=self.trip, display_name=n.title(),
                                              user=self.users[n], roles=roles,
                                              is_organizer=(n == "orga"))
        self.dest = Destination.objects.create(trip=self.trip, name="Chamrousse")
        self.lodging = Lodging.objects.create(destination=self.dest, name="Chalet Mélèze",
                                              proposed_by=self.m["owner"])

    def as_user(self, name):
        self.client.force_login(self.users[name])

    def test_any_member_can_propose_with_only_a_name(self):
        self.as_user("simple")
        r = self.client.post(reverse("lodging_add", args=[self.dest.pk]), {"name": "Studio"})
        self.assertEqual(r.status_code, 302)
        studio = Lodging.objects.get(name="Studio")
        self.assertEqual(studio.proposed_by, self.m["simple"])
        self.assertIsNone(studio.price)

    def test_unsafe_url_schemes_are_rejected(self):
        self.as_user("simple")
        for bad in ("javascript:alert(1)", "ftp://exemple.fr/chalet"):
            r = self.client.post(reverse("lodging_add", args=[self.dest.pk]),
                                 {"name": "Piège", "url": bad})
            self.assertEqual(r.status_code, 200, bad)
        self.assertFalse(Lodging.objects.filter(name="Piège").exists())

    def test_end_before_start_is_rejected(self):
        self.as_user("owner")
        r = self.client.post(reverse("lodging_edit", args=[self.lodging.pk]), {
            "name": "Chalet", "start_date": "2027-02-13", "end_date": "2027-02-06"})
        self.assertEqual(r.status_code, 200)

    def test_who_can_edit_and_delete(self):
        url = reverse("lodging_edit", args=[self.lodging.pk])
        for name in ("owner", "agent", "orga", "coorga"):
            self.as_user(name)
            self.assertEqual(self.client.get(url).status_code, 200, name)
        self.as_user("simple")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(reverse("lodging_delete",
                                                   args=[self.lodging.pk])).status_code, 403)
        self.as_user("agent")
        self.client.post(reverse("lodging_delete", args=[self.lodging.pk]))
        self.assertFalse(Lodging.objects.filter(pk=self.lodging.pk).exists())

    def test_fields_can_be_filled_in_later(self):
        self.as_user("agent")
        self.client.post(reverse("lodging_edit", args=[self.lodging.pk]), {
            "name": "Chalet Mélèze", "url": "https://exemple.fr/chalet", "price": "1200",
            "capacity": "6", "notes": "200 m des remontées", "start_date": "2027-02-06",
            "end_date": "2027-02-13"})
        self.lodging.refresh_from_db()
        self.assertEqual(self.lodging.price, Decimal("1200"))
        self.assertEqual(self.lodging.capacity, 6)

    def test_page_shows_price_per_person_and_capacity_warning(self):
        self.lodging.price, self.lodging.capacity = Decimal("1000"), 3
        self.lodging.save()
        self.as_user("simple")
        r = self.client.get(reverse("lodgings", args=[self.trip.pk]))
        self.assertContains(r, "200,00")        # 1000 € pour 5 participants
        self.assertContains(r, "moins que les 5 participants")

    def test_outsider_gets_404(self):
        self.as_user("stranger")
        self.assertEqual(self.client.get(reverse("lodgings", args=[self.trip.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("lodging_edit", args=[self.lodging.pk])).status_code, 404)


class AccountantAndAgentRoleTests(TestCase):
    def setUp(self):
        U = get_user_model()
        names = ("orga", "coorga", "accountant", "payer", "target")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test")
        self.m = {}
        for n in names:
            roles = {"coorga": ["coorganizer"], "accountant": ["accountant"]}.get(n, [])
            self.m[n] = Member.objects.create(trip=self.trip, display_name=n.title(),
                                              user=self.users[n], roles=roles,
                                              is_organizer=(n == "orga"))

    def expense(self, hidden=()):
        e = Expense.objects.create(trip=self.trip, paid_by=self.m["payer"],
                                   created_by=self.m["payer"], label="x",
                                   amount=Decimal("10"), split_mode="custom")
        e.hidden_from.set([self.m[h] for h in hidden])
        return e

    def test_accountant_can_edit_and_delete_visible_expenses(self):
        e = self.expense()
        self.client.force_login(self.users["accountant"])
        self.assertEqual(self.client.get(reverse("expense_edit", args=[e.pk])).status_code, 200)
        self.client.post(reverse("expense_delete", args=[e.pk]))
        self.assertFalse(Expense.objects.filter(pk=e.pk).exists())

    def test_accountant_cannot_touch_expenses_hidden_from_them(self):
        e = self.expense(hidden=["accountant"])
        self.client.force_login(self.users["accountant"])
        self.assertEqual(self.client.get(reverse("expense_edit", args=[e.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("expense_delete", args=[e.pk])).status_code, 404)
        self.assertTrue(Expense.objects.filter(pk=e.pk).exists())

    def test_accountant_cannot_manage_roles(self):
        self.client.force_login(self.users["accountant"])
        self.assertEqual(self.client.get(reverse("roles", args=[self.trip.pk])).status_code, 403)

    def test_coorganizer_assigns_new_roles_but_not_coorganizer(self):
        self.client.force_login(self.users["coorga"])
        self.client.post(reverse("roles", args=[self.trip.pk]), {
            f"roles_{self.m['target'].pk}": ["accountant", "agent", "coorganizer"]})
        self.m["target"].refresh_from_db()
        self.assertEqual(sorted(self.m["target"].roles), ["accountant", "agent"])


class DestinationPageTests(TestCase):
    def setUp(self):
        U = get_user_model()
        names = ("orga", "owner", "simple", "stranger")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test")
        self.m = {n: Member.objects.create(trip=self.trip, display_name=n.title(),
                                           user=self.users[n], is_organizer=(n == "orga"))
                  for n in names[:3]}
        self.cham = Destination.objects.create(
            trip=self.trip, name="Chamonix", url="https://exemple.fr/cham",
            notes="Vue sur le Mont-Blanc", proposed_by=self.m["owner"])
        self.lisbonne = Destination.objects.create(trip=self.trip, name="Lisbonne",
                                                   proposed_by=self.m["owner"])
        self.url = reverse("destination_list", args=[self.trip.pk])

    def as_user(self, name):
        self.client.force_login(self.users[name])

    def test_collapsed_line_shows_name_and_link_only_when_filled(self):
        self.as_user("simple")
        r = self.client.get(self.url)
        self.assertContains(r, '<summary><a href="https://exemple.fr/cham"')
        self.assertContains(r, "<summary>Lisbonne</summary>")

    def test_each_destination_is_a_foldable_block_with_details_inside(self):
        self.as_user("simple")
        r = self.client.get(self.url)
        self.assertContains(r, f'<details class="card accordion" id="dest-{self.cham.pk}">')
        self.assertContains(r, "Vue sur le Mont-Blanc")

    def test_trip_page_no_longer_lists_destinations(self):
        self.as_user("simple")
        r = self.client.get(reverse("trip_detail", args=[self.trip.pk]))
        self.assertNotContains(r, "Chamonix")
        self.assertContains(r, f'href="{self.url}"')

    def test_add_and_edit_return_to_the_page_with_the_block_open(self):
        self.as_user("simple")
        data = {"name": "Valence", "nearest_station": "", "url": "", "notes": "", "extra_text": ""}
        r = self.client.post(reverse("destination_add", args=[self.trip.pk]), data)
        valence = Destination.objects.get(name="Valence")
        self.assertRedirects(r, f"{self.url}#dest-{valence.pk}", fetch_redirect_response=False)
        r = self.client.post(reverse("destination_edit", args=[valence.pk]),
                             {**data, "notes": "Gare TGV"})
        self.assertRedirects(r, f"{self.url}#dest-{valence.pk}", fetch_redirect_response=False)

    def test_delete_rights_and_redirect(self):
        delete = reverse("destination_delete", args=[self.lisbonne.pk])
        self.as_user("simple")
        self.assertEqual(self.client.post(delete).status_code, 403)
        self.as_user("owner")
        r = self.client.post(delete)
        self.assertRedirects(r, self.url, fetch_redirect_response=False)
        self.assertFalse(Destination.objects.filter(pk=self.lisbonne.pk).exists())

    def test_delete_button_only_for_proposer_or_manager(self):
        self.as_user("simple")
        self.assertNotContains(self.client.get(self.url), "supprimer")
        self.as_user("orga")
        self.assertContains(self.client.get(self.url), "supprimer")

    def test_outsider_gets_404(self):
        self.as_user("stranger")
        self.assertEqual(self.client.get(self.url).status_code, 404)

class CalendarTests(SimpleTestCase):
    TODAY = date(2026, 11, 20)

    def test_window_starts_this_month(self):
        months, legend = build_calendar([], self.TODAY, months_count=6)
        self.assertEqual([m["title"] for m in months],
                         ["novembre 2026", "décembre 2026", "janvier 2027",
                          "février 2027", "mars 2027", "avril 2027"])
        self.assertEqual(legend, [])

    def test_offset_and_size(self):
        months, _ = build_calendar([], self.TODAY, months_count=3, offset=6)
        self.assertEqual([m["title"] for m in months], ["mai 2027", "juin 2027", "juillet 2027"])
        self.assertEqual(len(build_calendar([], self.TODAY, 24)[0]), 24)
        self.assertEqual(len(build_calendar([], self.TODAY, 1)[0]), 1)

    def test_cells_have_iso_label_and_past_flag(self):
        months, _ = build_calendar([], self.TODAY, 1)
        cells = {c["iso"]: c for w in months[0]["weeks"] for c in w if c}
        self.assertTrue(cells["2026-11-19"]["past"])
        self.assertFalse(cells["2026-11-20"]["past"])
        self.assertEqual(cells["2026-11-20"]["label"], "20 novembre 2026")
        self.assertEqual(len(cells), 30)

    def test_legend_keeps_all_periods_but_only_the_window_is_drawn(self):
        options = [SimpleNamespace(start_date=date(2026, 12, 24), end_date=date(2026, 12, 27)),
                   SimpleNamespace(start_date=date(2027, 8, 1), end_date=date(2027, 8, 5))]
        months, legend = build_calendar(options, self.TODAY, months_count=3)
        self.assertEqual([letter for letter, _ in legend], ["A", "B"])
        cells = {c["iso"]: c for m in months for w in m["weeks"] for c in w if c}
        self.assertEqual(cells["2026-12-25"]["letters"], ["A"])
        self.assertNotIn("2027-08-01", cells)

    def test_year_wrap(self):
        months, _ = build_calendar([], date(2026, 12, 5), months_count=2)
        self.assertEqual([m["title"] for m in months], ["décembre 2026", "janvier 2027"])

    def test_allow_past_option_of_check_period(self):
        today = date(2026, 11, 20)
        self.assertIn("passé", check_period(date(2026, 11, 1), date(2026, 11, 5), today))
        self.assertIsNone(check_period(date(2026, 11, 1), date(2026, 11, 5), today,
                                       allow_past=True))
        self.assertIn("après", check_period(date(2026, 11, 5), date(2026, 11, 1), today,
                                            allow_past=True))


class CalendarWindowTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.user = U.objects.create_user("anna", password="pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        Member.objects.create(trip=self.trip, display_name="Anna", user=self.user)
        self.url = reverse("date_list", args=[self.trip.pk])
        self.client.force_login(self.user)

    def tables(self, **params):
        html = self.client.get(self.url, params).content.decode()
        return html.count('<table class="cal">'), html

    def test_default_is_three_months(self):
        self.assertEqual(self.tables()[0], 3)

    def test_mois_parameter_sets_the_number_of_months(self):
        self.assertEqual(self.tables(mois=2)[0], 2)
        self.assertEqual(self.tables(mois=24)[0], 24)

    def test_invalid_values_fall_back_to_the_default(self):
        for bad in ("abc", "0", "25", "-1", "²", "999999999"):
            self.assertEqual(self.tables(mois=bad)[0], 3, bad)

    def test_choice_is_remembered_in_the_session(self):
        self.tables(mois=2)
        self.assertEqual(self.tables()[0], 2)

    def test_navigation_links_move_by_one_month(self):
        _, html = self.tables(mois=6)
        self.assertIn("decalage=1", html)
        self.assertNotIn("Mois précédent", html)
        _, html = self.tables(mois=6, decalage=5)
        self.assertIn("decalage=4", html)      # lien précédent
        self.assertIn("decalage=6", html)      # lien suivant
        _, html = self.tables(mois=6, decalage=24)
        self.assertNotIn("Mois suivant", html)
        self.assertIn("decalage=23", html)

    def test_next_shifts_the_window_by_exactly_one_month(self):
        def captions(**params):
            html = self.client.get(self.url, params).content.decode()
            return re.findall(r"<caption>(.*?)</caption>", html)

        first = captions(mois=3, decalage=0)
        shifted = captions(mois=3, decalage=1)
        self.assertEqual(shifted[:2], first[1:])

    def test_changing_the_number_of_months_keeps_the_start_month(self):
        _, html = self.tables(mois=6, decalage=3)
        self.assertIn('name="decalage" value="3"', html)

    def test_odd_offsets_do_not_crash(self):
        for bad in ("²", "x", "99999", "-4"):
            self.assertEqual(self.client.get(self.url, {"decalage": bad}).status_code, 200, bad)


class PeriodEditTests(TestCase):
    def setUp(self):
        U = get_user_model()
        names = ("orga", "coorga", "owner", "simple", "stranger")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test")
        self.m = {}
        for n in names[:-1]:
            roles = ["coorganizer"] if n == "coorga" else []
            self.m[n] = Member.objects.create(trip=self.trip, display_name=n.title(),
                                              user=self.users[n], roles=roles,
                                              is_organizer=(n == "orga"))
        self.base = djtz.localdate() + timedelta(days=30)
        self.opt = DateOption.objects.create(
            trip=self.trip, start_date=self.base, end_date=self.base + timedelta(days=7),
            proposed_by=self.m["owner"])
        self.url = reverse("date_edit", args=[self.opt.pk])

    def as_user(self, name):
        self.client.force_login(self.users[name])

    def dates(self, start_offset, end_offset, **extra):
        return {"start_date": (self.base + timedelta(days=start_offset)).isoformat(),
                "end_date": (self.base + timedelta(days=end_offset)).isoformat(), **extra}

    def test_who_can_open_the_edit_page(self):
        for name in ("owner", "orga", "coorga"):
            self.as_user(name)
            self.assertEqual(self.client.get(self.url).status_code, 200, name)
        self.as_user("simple")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.dates(1, 5)).status_code, 403)
        self.as_user("stranger")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_edit_updates_the_dates(self):
        self.as_user("owner")
        r = self.client.post(self.url, self.dates(2, 9))
        self.assertEqual(r.status_code, 302)
        self.opt.refresh_from_db()
        self.assertEqual(self.opt.start_date, self.base + timedelta(days=2))
        self.assertEqual(self.opt.end_date, self.base + timedelta(days=9))

    def test_votes_are_kept_unless_reset_is_requested(self):
        DateVote.objects.create(member=self.m["simple"], date_option=self.opt, score=2)
        self.as_user("owner")
        self.client.post(self.url, self.dates(2, 9))
        self.assertEqual(self.opt.votes.count(), 1)
        self.client.post(self.url, self.dates(3, 9, reset_votes="on"))
        self.assertEqual(self.opt.votes.count(), 0)

    def test_duplicate_period_is_rejected(self):
        other = DateOption.objects.create(
            trip=self.trip, start_date=self.base + timedelta(days=20),
            end_date=self.base + timedelta(days=25))
        self.as_user("owner")
        r = self.client.post(self.url, self.dates(20, 25))
        self.assertEqual(r.status_code, 200)
        self.opt.refresh_from_db()
        self.assertEqual(self.opt.start_date, self.base)
        self.assertTrue(DateOption.objects.filter(pk=other.pk).exists())

    def test_end_before_start_is_rejected(self):
        self.as_user("owner")
        self.assertEqual(self.client.post(self.url, self.dates(9, 2)).status_code, 200)

    def test_past_start_rules(self):
        past = djtz.localdate() - timedelta(days=10)
        old = DateOption.objects.create(trip=self.trip, start_date=past,
                                        end_date=past + timedelta(days=3),
                                        proposed_by=self.m["owner"])
        url = reverse("date_edit", args=[old.pk])
        self.as_user("owner")
        # garder le même début passé : accepté
        r = self.client.post(url, {"start_date": past.isoformat(),
                                   "end_date": (past + timedelta(days=5)).isoformat()})
        self.assertEqual(r.status_code, 302)
        # choisir un nouveau début passé : refusé
        r = self.client.post(url, {"start_date": (past + timedelta(days=1)).isoformat(),
                                   "end_date": (past + timedelta(days=5)).isoformat()})
        self.assertEqual(r.status_code, 200)

    def test_calendar_page_has_clickable_days_and_edit_link_for_allowed_members_only(self):
        list_url = reverse("date_list", args=[self.trip.pk])
        self.as_user("owner")
        page = self.client.get(list_url)
        self.assertContains(page, f'data-date="{self.base.isoformat()}"')
        self.assertContains(page, self.url)
        self.as_user("simple")
        self.assertNotContains(self.client.get(list_url), self.url)

    def test_picking_two_days_posts_the_same_fields_as_the_manual_form(self):
        self.as_user("simple")
        start = self.base + timedelta(days=40)
        r = self.client.post(reverse("date_list", args=[self.trip.pk]), {
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=5)).isoformat()})
        self.assertEqual(r.status_code, 302)
        created = DateOption.objects.get(start_date=start)
        self.assertEqual(created.proposed_by, self.m["simple"])


class PollLogicTests(SimpleTestCase):
    def test_parse_options(self):
        self.assertEqual(parse_options("Ski\n  rando \n\nSKI\nSpa"), ["Ski", "rando", "Spa"])
        for bad in ["", "Ski", "Ski\nski", "x" * 130 + "\ny",
                    "\n".join(str(i) for i in range(16))]:
            with self.assertRaises(ValueError):
                parse_options(bad)

    def test_votes_from_post(self):
        ids = [1, 2, 3]
        self.assertEqual(votes_from_post("single", QueryDict("choice=2"), ids), {2: 1})
        self.assertEqual(votes_from_post("single", QueryDict("choice=9"), ids), {})
        self.assertEqual(votes_from_post("single", QueryDict("choice="), ids), {})
        self.assertEqual(votes_from_post("single", QueryDict("choice=%C2%B2"), ids), {})
        self.assertEqual(votes_from_post(
            "multiple", QueryDict("choice=1&choice=3&choice=9&choice=x"), ids), {1: 1, 3: 1})
        self.assertEqual(votes_from_post("multiple", QueryDict(""), ids), {})
        self.assertEqual(votes_from_post(
            "scale", QueryDict("opt_1=2&opt_2=0&opt_3=&opt_9=1"), ids), {1: 2, 2: 0})
        self.assertEqual(votes_from_post("scale", QueryDict("opt_1=5"), ids), {})

    def test_tally_single_and_multiple(self):
        rows, voters = tally("single", [1, 2, 3], [(1, "a", 1), (1, "b", 1), (2, "c", 1)])
        self.assertEqual(voters, 3)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])
        self.assertEqual([r["percent"] for r in rows], [67, 33, 0])
        self.assertEqual([r["leader"] for r in rows], [True, False, False])
        rows, voters = tally("multiple", [1, 2], [(1, "a", 1), (2, "a", 1), (2, "b", 1)])
        self.assertEqual(voters, 2)
        self.assertEqual([(r["id"], r["percent"]) for r in rows], [(2, 100), (1, 50)])

    def test_tally_scale_and_empty(self):
        votes = [(1, "a", 2), (1, "b", 0), (2, "a", 1), (2, "b", 1), (3, "a", 0)]
        rows, voters = tally("scale", [1, 2, 3], votes)
        self.assertEqual(voters, 2)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])   # 2 points chacun : plus de Oui d'abord
        rows, voters = tally("single", [1, 2], [])
        self.assertEqual((voters, [r["leader"] for r in rows], [r["percent"] for r in rows]),
                         (0, [False, False], [None, None]))


class PollViewTests(TestCase):
    def setUp(self):
        U = get_user_model()
        names = ("orga", "maker", "voter", "other", "stranger")
        self.users = {n: U.objects.create_user(n, password="pw-test-123") for n in names}
        self.trip = Trip.objects.create(name="Test")
        labels = {"orga": "Orga", "maker": "Createur", "voter": "Votante", "other": "Autrui"}
        self.m = {n: Member.objects.create(trip=self.trip, display_name=labels[n],
                                           user=self.users[n], is_organizer=(n == "orga"))
                  for n in names[:4]}

    def as_user(self, name):
        self.client.force_login(self.users[name])

    def poll(self, kind="single", options=("Ski", "Rando", "Spa")):
        poll = Poll.objects.create(trip=self.trip, question="Activité ?", kind=kind,
                                   created_by=self.m["maker"])
        for n, text in enumerate(options):
            PollOption.objects.create(poll=poll, text=text, position=n)
        return poll

    def option_ids(self, poll):
        return list(poll.options.values_list("pk", flat=True))

    def test_any_member_can_create_a_poll(self):
        self.as_user("voter")
        r = self.client.post(reverse("poll_add", args=[self.trip.pk]), {
            "question": "Où manger ?", "description": "", "kind": "multiple",
            "options_text": "Crêperie\nPizzeria\nCrêperie\n"})
        poll = Poll.objects.get()
        self.assertRedirects(r, reverse("poll_detail", args=[poll.pk]))
        self.assertEqual(poll.kind, "multiple")
        self.assertEqual(poll.created_by, self.m["voter"])
        self.assertEqual([o.text for o in poll.options.all()], ["Crêperie", "Pizzeria"])

    def test_a_poll_needs_two_different_options(self):
        self.as_user("voter")
        r = self.client.post(reverse("poll_add", args=[self.trip.pk]), {
            "question": "Seul ?", "kind": "single", "options_text": "Seul"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Poll.objects.count(), 0)

    def test_single_choice_replaces_the_previous_vote(self):
        poll = self.poll()
        a, b, c = self.option_ids(poll)
        self.as_user("voter")
        url = reverse("poll_vote", args=[poll.pk])
        self.client.post(url, {"choice": a})
        self.client.post(url, {"choice": b})
        votes = PollVote.objects.filter(member=self.m["voter"])
        self.assertEqual([v.option_id for v in votes], [b])
        self.client.post(url, {"choice": ""})
        self.assertEqual(PollVote.objects.filter(member=self.m["voter"]).count(), 0)

    def test_multiple_choice_ignores_unknown_options(self):
        poll = self.poll("multiple")
        a, b, c = self.option_ids(poll)
        self.as_user("voter")
        self.client.post(reverse("poll_vote", args=[poll.pk]), {"choice": [a, c, 99999]})
        self.assertEqual(set(PollVote.objects.values_list("option_id", flat=True)), {a, c})

    def test_scale_votes(self):
        poll = self.poll("scale")
        a, b, c = self.option_ids(poll)
        self.as_user("voter")
        self.client.post(reverse("poll_vote", args=[poll.pk]),
                         {f"opt_{a}": "2", f"opt_{b}": "0", f"opt_{c}": ""})
        scores = dict(PollVote.objects.values_list("option_id", "score"))
        self.assertEqual(scores, {a: 2, b: 0})

    def test_options_of_another_poll_are_ignored(self):
        first, second = self.poll(), self.poll()
        foreign = self.option_ids(second)[0]
        self.as_user("voter")
        self.client.post(reverse("poll_vote", args=[first.pk]), {"choice": foreign})
        self.assertEqual(PollVote.objects.count(), 0)

    def test_closed_poll_rejects_votes(self):
        poll = self.poll()
        poll.is_closed = True
        poll.save()
        self.as_user("voter")
        self.client.post(reverse("poll_vote", args=[poll.pk]),
                         {"choice": self.option_ids(poll)[0]})
        self.assertEqual(PollVote.objects.count(), 0)

    def test_results_are_visible_but_never_who_voted(self):
        poll = self.poll()
        a, b, c = self.option_ids(poll)
        for name, option in (("voter", a), ("other", b)):
            self.as_user(name)
            self.client.post(reverse("poll_vote", args=[poll.pk]), {"choice": option})
        self.as_user("maker")
        page = self.client.get(reverse("poll_detail", args=[poll.pk]))
        self.assertContains(page, "2 votants")
        self.assertContains(page, "50 %")
        self.assertNotContains(page, "Votante")
        self.assertNotContains(page, "Autrui")

    def test_only_creator_or_manager_can_manage(self):
        poll = self.poll()
        for name in ("voter", "other"):
            self.as_user(name)
            self.assertEqual(self.client.get(reverse("poll_edit", args=[poll.pk])).status_code, 403)
            for action in ("poll_toggle", "poll_delete", "poll_option_add"):
                self.assertEqual(self.client.post(reverse(action, args=[poll.pk]),
                                                  {"text": "Piège"}).status_code, 403, action)
        poll.refresh_from_db()
        self.assertFalse(poll.is_closed)
        for name in ("maker", "orga"):
            self.as_user(name)
            self.client.post(reverse("poll_toggle", args=[poll.pk]))
            poll.refresh_from_db()
            self.assertTrue(poll.is_closed, name)
            self.client.post(reverse("poll_toggle", args=[poll.pk]))

    def test_option_management(self):
        poll = self.poll(options=("Ski", "Rando"))
        self.as_user("maker")
        add = reverse("poll_option_add", args=[poll.pk])
        self.client.post(add, {"text": "Spa"})
        self.client.post(add, {"text": "spa"})            # doublon
        self.assertEqual(poll.options.count(), 3)
        spa = poll.options.get(text="Spa")
        PollVote.objects.create(option=spa, member=self.m["voter"])
        self.client.post(reverse("poll_option_delete", args=[spa.pk]))
        self.assertEqual(poll.options.count(), 2)
        self.assertEqual(PollVote.objects.count(), 0)     # les votes partent avec l'option
        self.client.post(reverse("poll_option_delete", args=[poll.options.first().pk]))
        self.assertEqual(poll.options.count(), 2)         # minimum deux options

    def test_option_limit(self):
        poll = self.poll(options=tuple(f"Option {i}" for i in range(15)))
        self.as_user("maker")
        self.client.post(reverse("poll_option_add", args=[poll.pk]), {"text": "Une de trop"})
        self.assertEqual(poll.options.count(), 15)

    def test_outsider_gets_404(self):
        poll = self.poll()
        self.as_user("stranger")
        self.assertEqual(self.client.get(reverse("polls", args=[self.trip.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("poll_detail", args=[poll.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("poll_vote", args=[poll.pk]),
                                          {"choice": 1}).status_code, 404)

