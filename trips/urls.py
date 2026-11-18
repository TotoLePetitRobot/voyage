from django.urls import path
from . import views

urlpatterns = [
    path("", views.trip_list, name="trip_list"),
    path("voyages/nouveau/", views.trip_create, name="trip_create"),
    path("voyages/<int:pk>/", views.trip_detail, name="trip_detail"),
    path("rejoindre/<str:token>/", views.trip_join, name="trip_join"),
    path("voyages/<int:pk>/destinations/nouvelle/", views.destination_add, name="destination_add"),
    path("voyages/<int:pk>/destinations/import/", views.destination_import, name="destination_import"),
    path("destinations/<int:dest_pk>/modifier/", views.destination_edit, name="destination_edit"),
    path("destinations/<int:dest_pk>/supprimer/", views.destination_delete, name="destination_delete"),
    path("voyages/<int:pk>/dates/", views.date_list, name="date_list"),
    path("dates/<int:opt_pk>/supprimer/", views.date_delete, name="date_delete"),
    path("voyages/<int:pk>/votes/", views.votes, name="votes"),
    path("voyages/<int:pk>/synthese/", views.trip_summary, name="trip_summary"),
    path("voyages/<int:pk>/budget/", views.budget, name="budget"),
    path("voyages/<int:pk>/budget/depense/nouvelle/", views.expense_add, name="expense_add"),
    path("voyages/<int:pk>/budget/rembourser/", views.settle, name="settle"),
    path("depenses/<int:expense_pk>/modifier/", views.expense_edit, name="expense_edit"),
    path("depenses/<int:expense_pk>/supprimer/", views.expense_delete, name="expense_delete"),
    path("voyages/<int:pk>/solidarite/", views.solidarity, name="solidarity"),
    path("voyages/<int:pk>/destinations/<int:dest_pk>/recherche/", views.search, name="search"),
]
