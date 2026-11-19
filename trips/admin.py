from django.contrib import admin
from .models import Trip, Meal, Member, Destination, DateOption, Expense

admin.site.register(Trip)
admin.site.register(Meal)
admin.site.register(Member)
admin.site.register(Destination)
admin.site.register(DateOption)
admin.site.register(Expense)
