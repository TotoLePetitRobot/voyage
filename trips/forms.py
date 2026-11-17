from django import forms
from .models import Trip, Member

class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ["name"]

class MemberForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ["display_name", "origin_city"]
