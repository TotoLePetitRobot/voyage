from django.urls import path
from . import views

urlpatterns = [
    path("", views.trip_list, name="trip_list"),
    path("voyages/nouveau/", views.trip_create, name="trip_create"),
    path("voyages/<int:pk>/", views.trip_detail, name="trip_detail"),
    path("rejoindre/<str:token>/", views.trip_join, name="trip_join"),
]
