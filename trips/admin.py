from django.contrib import admin
from .models import (Trip, Lodging, Leg, Meal, Member, Destination, DateOption, 
                     Expense, Poll, DateVote, DestVote, LodgingVote, Comment, Feedback)

admin.site.register(Trip)
admin.site.register(Lodging)
admin.site.register(Leg)
admin.site.register(Meal)
admin.site.register(Member)
admin.site.register(Destination)
admin.site.register(DateOption)
admin.site.register(Expense)
admin.site.register(Poll)
admin.site.register(DateVote)
admin.site.register(DestVote)
admin.site.register(LodgingVote)
@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "page_name", "path", "short_message", "handled")
    list_filter = ("handled", "page_name")
    list_editable = ("handled",)
    search_fields = ("message", "path", "user__username")
    date_hierarchy = "created_at"

    @admin.display(description="Message")
    def short_message(self, obj):
        return obj.message[:80]


admin.site.register(Comment)
