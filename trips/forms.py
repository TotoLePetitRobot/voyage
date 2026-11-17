from datetime import timedelta

from django import forms
from django.utils import timezone

from .models import DateOption, Destination, Member, Trip

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
