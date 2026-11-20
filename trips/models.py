import secrets
from django.conf import settings
from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from decimal import Decimal
from django.utils import timezone

from .shopping import format_line

ROLE_COORGANIZER = "coorganizer"
ROLE_CHEF = "chef"
ROLE_CHOICES = [(ROLE_COORGANIZER, "Co-organisateur"), (ROLE_CHEF, "Chef cuistot")]
ROLE_LABELS = dict(ROLE_CHOICES)

def make_token():
    return secrets.token_urlsafe(16)

class Trip(models.Model):
    name = models.CharField("Nom du voyage", max_length=120)
    invite_token = models.CharField(max_length=32, unique=True, default=make_token)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Member(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="memberships")
    display_name = models.CharField("Nom affiché", max_length=60, blank=True)
    origin_city = models.CharField("Ville de départ", max_length=100, blank=True)
    return_city = models.CharField("Lieu de retour (si différent du départ)",
                                   max_length=100, blank=True)
    budget = models.DecimalField(
        "Budget envisagé (€ par personne)", max_digits=8, decimal_places=2,
        null=True, blank=True, validators=[MinValueValidator(0)],
    )
    budget_anonymous = models.BooleanField(
        "Garder mon budget anonyme", default=False,
        help_text="Il ne sera pas affiché avec votre nom. Il compte seulement dans une "
                  "statistique de groupe, visible à partir de 3 budgets anonymes.",
    )
    pay_cap = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                  validators=[MinValueValidator(0)])
    extra_max = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                    validators=[MinValueValidator(0)])
    roles = models.JSONField(default=list, blank=True)
    is_organizer = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["trip", "user"], name="unique_member_per_trip")
        ]

    @property
    def label(self):
        return self.display_name.strip() or "Nouveau membre"

    @property
    def can_manage(self):
        return self.is_organizer or ROLE_COORGANIZER in self.roles

    @property
    def is_chef(self):
        return ROLE_CHEF in self.roles

    @property
    def role_labels(self):
        labels = ["Organisateur"] if self.is_organizer else []
        return labels + [ROLE_LABELS[r] for r in self.roles if r in ROLE_LABELS]

    def can_edit_meal(self, meal):
        return self.can_manage or self.is_chef or meal.proposed_by_id == self.pk

    def assignable_roles(self):
        everything = {code for code, _ in ROLE_CHOICES}
        if self.is_organizer:
            return everything
        if ROLE_COORGANIZER in self.roles:
            return everything - {ROLE_COORGANIZER}
        return set()

    def __str__(self):
        return f"{self.label} – {self.trip}"

class Destination(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="destinations")
    name = models.CharField("Destination", max_length=120)
    nearest_station = models.TextField(
        "Gares les plus proches", blank=True,
        help_text="Une par ligne, 5 maximum. Exemple : Grenoble puis Valence TGV.",
    )    
    url = models.URLField("Lien", blank=True)
    notes = models.TextField("Notes", blank=True)
    extra_info = models.JSONField(default=dict, blank=True)
    proposed_by = models.ForeignKey(Member, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="proposed_destinations")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    @property
    def extra_pairs(self):
        return list(self.extra_info.items())

    @property
    def stations(self):
        seen, out = set(), []
        for line in self.nearest_station.splitlines():
            name = " ".join(line.split())
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                out.append(name)
        return out[:5]

    def __str__(self):
        return self.name

SCORE_CHOICES = [(2, "Oui"), (1, "Peut-être"), (0, "Non")]


class DateOption(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="date_options")
    start_date = models.DateField("Arrivée")
    end_date = models.DateField("Départ")
    proposed_by = models.ForeignKey(Member, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="proposed_dates")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date", "end_date"]

    @property
    def nights(self):
        return (self.end_date - self.start_date).days

    def __str__(self):
        return f"{self.start_date} → {self.end_date}"


class DestVote(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="dest_votes")
    destination = models.ForeignKey(Destination, on_delete=models.CASCADE, related_name="votes")
    score = models.PositiveSmallIntegerField(choices=SCORE_CHOICES)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["member", "destination"], name="unique_dest_vote")
        ]


class DateVote(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="date_votes")
    date_option = models.ForeignKey(DateOption, on_delete=models.CASCADE, related_name="votes")
    score = models.PositiveSmallIntegerField(choices=SCORE_CHOICES)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["member", "date_option"], name="unique_date_vote")
        ]


CATEGORY_CHOICES = [
    ("logement", "Logement"),
    ("transport", "Transport"),
    ("nourriture", "Nourriture"),
    ("activites", "Activités"),
    ("autre", "Autre"),
]
class Expense(models.Model):
    SPLIT_CHOICES = [("equal", "Parts égales"), ("custom", "Montants personnalisés")]

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="expenses")
    paid_by = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="paid_expenses")
    created_by = models.ForeignKey(Member, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="created_expenses")
    label = models.CharField("Description", max_length=120)
    category = models.CharField("Catégorie", max_length=12, choices=CATEGORY_CHOICES,
                                default="autre")
    amount = models.DecimalField("Montant (€)", max_digits=10, decimal_places=2,
                                 validators=[MinValueValidator(Decimal("0.01"))])
    date = models.DateField(default=timezone.localdate)
    split_mode = models.CharField(max_length=10, choices=SPLIT_CHOICES, default="equal")
    is_transfer = models.BooleanField(default=False)
    hidden_from = models.ManyToManyField("Member", blank=True, related_name="hidden_expenses")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.label} ({self.amount} €)"


class ExpenseShare(models.Model):
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="shares")
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="expense_shares")
    amount = models.DecimalField(max_digits=10, decimal_places=2,
                                 validators=[MinValueValidator(0)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["expense", "member"], name="unique_share_per_member")
        ]

class SolidarityPref(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE,
                               related_name="solidarity_prefs")
    category = models.CharField(max_length=12, choices=CATEGORY_CHOICES)
    weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.10")), MaxValueValidator(Decimal("5"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["member", "category"], name="unique_pref_per_category")
        ]

MEAL_TYPES = [(1, "Petit-déjeuner"), (2, "Déjeuner"), (3, "Goûter"), (4, "Dîner"), (5, "Autre")]


class Meal(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="meals")
    title = models.CharField("Repas", max_length=120)
    day = models.PositiveSmallIntegerField(
        "Jour n°", default=1, validators=[MinValueValidator(1), MaxValueValidator(60)],
        help_text="1 = premier jour du séjour.")
    meal_type = models.PositiveSmallIntegerField("Type", choices=MEAL_TYPES, default=2)
    recipe = models.TextField("Recette (facultatif)", blank=True)
    proposed_by = models.ForeignKey(Member, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="proposed_meals")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["day", "meal_type", "created_at"]

    def __str__(self):
        return f"J{self.day} – {self.title}"


class MealItem(models.Model):
    meal = models.ForeignKey(Meal, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=120)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=3, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    @property
    def display(self):
        return format_line(self.name, self.quantity, self.unit)

TRANSPORT_CHOICES = [("train", "Train"), ("car", "Voiture"), ("shuttle", "Navette ou bus"),
                     ("plane", "Avion"), ("other", "Autre")]
LEG_STATUS_CHOICES = [("planned", "Prévu"), ("confirmed", "Confirmé")]
DIRECTION_CHOICES = [("out", "Aller"), ("back", "Retour")]


class Leg(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="legs")
    direction = models.CharField("Sens", max_length=4, choices=DIRECTION_CHOICES, default="out")
    position = models.PositiveSmallIntegerField(
        "Étape n°", default=1, validators=[MinValueValidator(1), MaxValueValidator(20)])
    name = models.CharField("Nom", max_length=120, help_text="Exemple : Train Paris–Grenoble")
    transport = models.CharField("Type de transport", max_length=8,
                                 choices=TRANSPORT_CHOICES, default="train")
    departure_place = models.CharField("Lieu de départ", max_length=120)
    departure_at = models.DateTimeField("Heure de départ", null=True, blank=True)
    arrival_place = models.CharField("Lieu d’arrivée", max_length=120)
    arrival_at = models.DateTimeField("Heure d’arrivée", null=True, blank=True)
    price = models.DecimalField("Prix par personne (€, facultatif)", max_digits=8,
                                decimal_places=2, null=True, blank=True,
                                validators=[MinValueValidator(0)])
    status = models.CharField("Statut", max_length=9, choices=LEG_STATUS_CHOICES,
                              default="planned")
    seats = models.PositiveSmallIntegerField(
        "Places disponibles (voiture)", null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(20)])
    proposed_by = models.ForeignKey(Member, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="proposed_legs")
    passengers = models.ManyToManyField(Member, blank=True, related_name="rides")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "departure_at", "id"]

    @property
    def is_car(self):
        return self.transport == "car"

    @property
    def seats_left(self):
        if not self.is_car or self.seats is None:
            return None
        return max(self.seats - len(self.passengers.all()), 0)

    def __str__(self):
        return self.name
