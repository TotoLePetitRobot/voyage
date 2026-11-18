import random
import sqlite3
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .backup import backup_sqlite
from .links import lodging_links, sncf_phrase, train_links
from .models import (DateOption, Destination, Expense, ExpenseShare, Member, 
                     SolidarityPref, Trip)
from .settlement import allocate, compute_owed, equal_split, suggest_transfers
from .views import trip_balances


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
        self.ma = Member.objects.create(trip=self.trip, user=self.a, is_organizer=True)
        self.mb = Member.objects.create(trip=self.trip, user=self.b)

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
            self.m.append(Member.objects.create(trip=self.trip, user=user, is_organizer=(i == 0)))

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
        Member.objects.create(trip=self.trip, user=self.a, origin_city="Lyon")
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
