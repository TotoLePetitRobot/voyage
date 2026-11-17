from datetime import timedelta
from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import DateOption, Destination, Expense, Member, Trip
from .settlement import equal_split, to_cents

class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ["name"]


class MemberForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ["display_name", "origin_city", "budget", "budget_anonymous"]


class DestinationForm(forms.ModelForm):
    extra_text = forms.CharField(
        label="Infos spécifiques",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Une par ligne, au format « clé : valeur ». Exemple : Forfait 6 jours : 320 €",
    )

    class Meta:
        model = Destination
        fields = ["name", "url", "notes"]

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
            today = timezone.localdate()
            if end < start:
                raise forms.ValidationError("La date de départ doit être après l’arrivée.")
            if (end - start).days > 60:
                raise forms.ValidationError("Une période ne peut pas dépasser 60 nuits.")
            if start < today:
                raise forms.ValidationError("La période ne peut pas commencer dans le passé.")
            if start > today + timedelta(days=730):
                raise forms.ValidationError("Choisissez une date dans les deux prochaines années.")
        return cleaned

class ExpenseForm(forms.Form):
    label = forms.CharField(label="Description", max_length=120)
    amount = forms.DecimalField(
        label="Montant (€)", max_digits=10, decimal_places=2,
        min_value=Decimal("0.01"), max_value=Decimal("100000"),
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
    )
    date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    paid_by = forms.ModelChoiceField(queryset=Member.objects.none(), label="Payé par")
    split_mode = forms.ChoiceField(label="Répartition", choices=Expense.SPLIT_CHOICES,
                                   widget=forms.RadioSelect, initial="equal")
    participants = forms.ModelMultipleChoiceField(
        queryset=Member.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple, label="Parts égales entre",
    )

    def __init__(self, *args, trip, **kwargs):
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

    @property
    def main_fields(self):
        return [self[n] for n in ("label", "amount", "date", "paid_by", "split_mode", "participants")]

    @property
    def share_fields(self):
        return [self[f"share_{m.pk}"] for m in self.members]

    def clean(self):
        cleaned = super().clean()
        amount, mode = cleaned.get("amount"), cleaned.get("split_mode")
        if amount is None or mode is None:
            return cleaned
        total = to_cents(amount)
        if mode == "equal":
            parts = cleaned.get("participants")
            if not parts:
                raise forms.ValidationError("Choisissez au moins un participant.")
            shares = equal_split(total, [p.pk for p in parts])
        else:
            shares = {}
            for m in self.members:
                value = cleaned.get(f"share_{m.pk}")
                if value and to_cents(value) > 0:
                    shares[m.pk] = to_cents(value)
            if sum(shares.values()) != total:
                raise forms.ValidationError(
                    f"La somme des parts ({sum(shares.values()) / 100:.2f} €) "
                    f"doit être égale au montant ({total / 100:.2f} €)."
                )
        cleaned["shares_cents"] = shares
        return cleaned
