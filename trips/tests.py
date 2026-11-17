import random

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Expense, Member, Trip
from .settlement import equal_split, suggest_transfers
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
        self.a = U.objects.create_user("a@example.com", "pw-test-123")
        self.b = U.objects.create_user("b@example.com", "pw-test-123")
        self.c = U.objects.create_user("c@example.com", "pw-test-123")
        self.trip = Trip.objects.create(name="Test")
        self.ma = Member.objects.create(trip=self.trip, user=self.a, is_organizer=True)
        self.mb = Member.objects.create(trip=self.trip, user=self.b)

    def add_expense(self, amount="30.00"):
        return self.client.post(reverse("expense_add", args=[self.trip.pk]), {
            "label": "Courses", "amount": amount, "date": "2026-11-20",
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
