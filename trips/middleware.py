from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect


class PseudoRequired(Exception):
    def __init__(self, trip_pk):
        super().__init__("Pseudo requis")
        self.trip_pk = trip_pk


class LoginRedirect(Exception):
    """Visiteur anonyme sur une page du voyage."""


class PseudoRequiredMiddleware:
    """Transforme certaines exceptions des vues en redirections.

    Django convertit les exceptions des vues en réponses avant qu'elles n'atteignent
    __call__ : seule process_exception peut les intercepter."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, PseudoRequired):
            return redirect("trip_pseudo", pk=exception.trip_pk)
        if isinstance(exception, LoginRedirect):
            return redirect_to_login(request.get_full_path())
        return None
