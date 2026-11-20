from datetime import timedelta
from decimal import Decimal

from django import forms
from django.utils import timezone
from django.core.validators import URLValidator

from .models import (CATEGORY_CHOICES, DateOption, Destination, Expense, Meal, 
                     Member, Trip, Leg, Lodging)
from .periods import check_period
from .settlement import equal_split, to_cents
from .shopping import format_line, parse_line

class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ["name"]


class PseudoForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ["display_name"]
        labels = {"display_name": "Votre pseudo dans ce voyage"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["display_name"].required = True

    def clean_display_name(self):
        name = " ".join(self.cleaned_data["display_name"].split())
        if len(name) < 2:
            raise forms.ValidationError("Au moins 2 caractères.")
        if len(name) > 30:
            raise forms.ValidationError("30 caractères au maximum.")
        clash = Member.objects.filter(trip=self.instance.trip, display_name__iexact=name)
        if clash.exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Ce pseudo est déjà utilisé dans ce voyage.")
        return name


class MemberForm(PseudoForm):
    class Meta(PseudoForm.Meta):
        fields = ["display_name", "origin_city", "return_city", "budget", "budget_anonymous"]


class DestinationForm(forms.ModelForm):
    extra_text = forms.CharField(
        label="Infos spécifiques",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Une par ligne, au format « clé : valeur ». Exemple : Forfait 6 jours : 320 €",
    )

    class Meta:
        model = Destination
        fields = ["name", "nearest_station", "url", "notes"]
        widgets = {"nearest_station": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["extra_text"].initial = "\n".join(
                f"{k} : {v}" for k, v in self.instance.extra_info.items()
            )

    def clean_extra_text(self):
        data = {}
        for n, line in enumerate(self.cleaned_data["extra_text"].splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            if ":" not in line:
                raise forms.ValidationError(f"Ligne {n} : format « clé : valeur » attendu.")
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if not key:
                raise forms.ValidationError(f"Ligne {n} : la clé est vide.")
            data[key] = value
        return data

    def save(self, commit=True):
        self.instance.extra_info = self.cleaned_data["extra_text"]
        return super().save(commit)

    def clean_nearest_station(self):
        lines = []
        for line in self.cleaned_data["nearest_station"].splitlines():
            name = " ".join(line.split())
            if name and name.casefold() not in {l.casefold() for l in lines}:
                lines.append(name)
        if len(lines) > 5:
            raise forms.ValidationError("5 gares au maximum.")
        if any(len(l) > 120 for l in lines):
            raise forms.ValidationError("120 caractères au maximum par gare.")
        return "\n".join(lines)


class ImportForm(forms.Form):
    file = forms.FileField(label="Fichier CSV")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith(".csv"):
            raise forms.ValidationError("Choisissez un fichier .csv.")
        if f.size > 1_000_000:
            raise forms.ValidationError("Fichier trop volumineux (1 Mo maximum).")
        return f

class DateOptionForm(forms.ModelForm):
    start_date = forms.DateField(label="Arrivée", widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(label="Départ", widget=forms.DateInput(attrs={"type": "date"}))

    class Meta:
        model = DateOption
        fields = ["start_date", "end_date"]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end:
            problem = check_period(start, end, timezone.localdate())
            if problem:
                raise forms.ValidationError(problem)
        return cleaned

class ExpenseForm(forms.Form):
    label = forms.CharField(label="Description", max_length=120)
    amount = forms.DecimalField(
        label="Montant (€)", max_digits=10, decimal_places=2,
        min_value=Decimal("0.01"), max_value=Decimal("100000"),
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )
    category = forms.ChoiceField(label="Catégorie", choices=CATEGORY_CHOICES, initial="autre")
    date = forms.DateField(label="Date",
                           widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    paid_by = forms.ModelChoiceField(queryset=Member.objects.none(), label="Payé par")
    split_mode = forms.ChoiceField(label="Répartition", choices=Expense.SPLIT_CHOICES,
                                   widget=forms.RadioSelect, initial="equal")
    participants = forms.ModelMultipleChoiceField(
        queryset=Member.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple, label="Parts égales entre",
    )

    def __init__(self, *args, trip, viewer=None, fixed_hidden=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = trip.members.select_related("user").order_by("id")
        self.members = list(qs)
        for name in ("paid_by", "participants"):
            self.fields[name].queryset = qs
            self.fields[name].label_from_instance = lambda m: m.label
        for m in self.members:
            self.fields[f"share_{m.pk}"] = forms.DecimalField(
                label=m.label, required=False, min_value=Decimal("0"),
                max_digits=10, decimal_places=2,
                widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            )
        # fixed_hidden is None : création (choix possible) ; sinon : édition (audience figée)
        self.fixed_hidden = set(fixed_hidden or ())
        if fixed_hidden is None:
            choices = qs.exclude(pk=viewer.pk) if viewer is not None else qs.none()
            field = forms.ModelMultipleChoiceField(
                queryset=choices, required=False, widget=forms.CheckboxSelectMultiple,
                label="Cacher à (par exemple le destinataire d’un cadeau)",
                help_text="Ces personnes ne verront ni cette dépense ni les remboursements "
                          "qui s’y rattachent, quel que soit leur rôle. Elles n’y participent "
                          "pas. Modifiable seulement en supprimant puis recréant la dépense.",
            )
            field.label_from_instance = lambda m: m.label
            self.fields["hidden_from"] = field

    @property
    def main_fields(self):
        names = ["label", "amount", "category", "date", "paid_by", "split_mode", "participants"]
        if "hidden_from" in self.fields:
            names.append("hidden_from")
        return [self[n] for n in names]

    @property
    def share_fields(self):
        return [self[f"share_{m.pk}"] for m in self.members]

    def hidden_ids(self, cleaned):
        if "hidden_from" in self.fields:
            return {m.pk for m in cleaned.get("hidden_from") or []}
        return set(self.fixed_hidden)

    def clean(self):
        cleaned = super().clean()
        amount, mode = cleaned.get("amount"), cleaned.get("split_mode")
        if amount is None or mode is None:
            return cleaned
        hidden = self.hidden_ids(cleaned)
        payer = cleaned.get("paid_by")
        if hidden and payer is not None and payer.pk in hidden:
            raise forms.ValidationError(
                "Le payeur ne peut pas être un destinataire de la dépense cachée.")
        total = to_cents(amount)
        if mode == "equal":
            parts = [p for p in (cleaned.get("participants") or []) if p.pk not in hidden]
            if not parts:
                raise forms.ValidationError(
                    "Choisissez au moins un participant (les destinataires d’une dépense "
                    "cachée n’y participent jamais).")
            shares = equal_split(total, [p.pk for p in parts])
        else:
            shares = {}
            for m in self.members:
                value = cleaned.get(f"share_{m.pk}")
                if value and to_cents(value) > 0:
                    shares[m.pk] = to_cents(value)
            if hidden & set(shares):
                raise forms.ValidationError(
                    "Les destinataires d’une dépense cachée ne peuvent pas avoir de part.")
            if sum(shares.values()) != total:
                raise forms.ValidationError(
                    f"La somme des parts ({sum(shares.values()) / 100:.2f} €) "
                    f"doit être égale au montant ({total / 100:.2f} €)."
                )
        cleaned["shares_cents"] = shares
        return cleaned

class SolidarityForm(forms.Form):
    pay_cap = forms.DecimalField(
        label="Plafond : je ne peux pas payer plus de (€ au total)", required=False,
        min_value=Decimal("0"), max_digits=8, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    extra_max = forms.DecimalField(
        label="Je peux prendre en charge jusqu’à (€ en plus)", required=False,
        min_value=Decimal("0"), max_digits=8, decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )

    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        prefs = {p.category: p.weight for p in member.solidarity_prefs.all()}
        self.cats = []
        for code, label in CATEGORY_CHOICES:
            self.fields[f"opt_{code}"] = forms.BooleanField(
                required=False, initial=code in prefs, label=label)
            self.fields[f"w_{code}"] = forms.DecimalField(
                required=False, min_value=Decimal("0.10"), max_value=Decimal("5"),
                max_digits=4, decimal_places=2, initial=prefs.get(code, Decimal("1.00")),
                widget=forms.NumberInput(attrs={"step": "0.05", "min": "0.1", "max": "5"}),
            )
            self.cats.append((label, self[f"opt_{code}"], self[f"w_{code}"]))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("pay_cap") is not None and cleaned.get("extra_max") is not None:
            raise forms.ValidationError(
                "Choisissez soit un plafond, soit une capacité de prise en charge, pas les deux.")
        for code, _ in CATEGORY_CHOICES:
            if cleaned.get(f"opt_{code}") and cleaned.get(f"w_{code}") is None:
                self.add_error(f"w_{code}", "Indiquez un poids (1 = part normale).")
        return cleaned

class MealForm(forms.ModelForm):
    items_text = forms.CharField(
        label="Liste de courses (facultatif)", required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="Un article par ligne, quantité facultative. Exemples : « 500 g de riz », "
                  "« 3 tomates », « sel ». Unités reconnues : g, kg, ml, cl, dl, l.",
    )

    class Meta:
        model = Meal
        fields = ["title", "day", "meal_type", "recipe"]
        widgets = {"recipe": forms.Textarea(attrs={"rows": 6})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items_parsed = []
        if self.instance.pk:
            self.fields["items_text"].initial = "\n".join(
                format_line(i.name, i.quantity, i.unit) for i in self.instance.items.all())

    def clean_recipe(self):
        recipe = self.cleaned_data["recipe"].strip()
        if len(recipe) > 5000:
            raise forms.ValidationError("5000 caractères au maximum.")
        return recipe

    def clean_items_text(self):
        text = self.cleaned_data["items_text"]
        parsed = []
        for number, line in enumerate(text.splitlines(), 1):
            try:
                item = parse_line(line)
            except ValueError as error:
                raise forms.ValidationError(f"Ligne {number} : {error}.")
            if item:
                parsed.append(item)
        if len(parsed) > 60:
            raise forms.ValidationError("60 articles au maximum par repas.")
        self.items_parsed = parsed
        return text

class LegForm(forms.ModelForm):
    class Meta:
        model = Leg
        fields = ["direction", "position", "name", "transport", "departure_place",
                  "departure_at", "arrival_place", "arrival_at", "price", "status", "seats"]
        widgets = {
            "departure_at": forms.DateTimeInput(attrs={"type": "datetime-local"},
                                                format="%Y-%m-%dT%H:%M"),
            "arrival_at": forms.DateTimeInput(attrs={"type": "datetime-local"},
                                              format="%Y-%m-%dT%H:%M"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("departure_at", "arrival_at"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                                               "%Y-%m-%d %H:%M"]
        self.fields["price"].widget.attrs.update(step="0.01", min="0")
        self.fields["seats"].help_text = ("Seulement pour la voiture : nombre de places "
                                          "libres pour les autres membres.")

    def clean(self):
        cleaned = super().clean()
        dep, arr = cleaned.get("departure_at"), cleaned.get("arrival_at")
        if dep and arr and arr < dep:
            self.add_error("arrival_at", "L’arrivée doit être après le départ.")
        transport, seats = cleaned.get("transport"), cleaned.get("seats")
        passengers = self.instance.passengers.count() if self.instance.pk else 0
        if transport == "car":
            if seats is None:
                self.add_error("seats", "Indiquez les places disponibles pour une voiture.")
            elif seats < passengers:
                self.add_error("seats", f"{passengers} passager(s) déjà inscrit(s) : "
                                        "retirez-en d’abord.")
        elif transport:
            cleaned["seats"] = None
            if passengers:
                self.add_error("transport", "Retirez d’abord les passagers pour changer "
                                            "de type de transport.")
        return cleaned

_HTTP_URL = URLValidator(schemes=["http", "https"])


class LodgingForm(forms.ModelForm):
    class Meta:
        model = Lodging
        fields = ["name", "url", "start_date", "end_date", "price", "capacity", "notes"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["price"].widget.attrs.update(step="0.01", min="0")
        self.fields["notes"].help_text = (
            "Par exemple : distance aux remontées mécaniques, étage, parking…")

    def clean_url(self):
        url = self.cleaned_data["url"].strip()
        if url:
            _HTTP_URL(url)
        return url

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "La fin doit être après le début.")
        return cleaned
