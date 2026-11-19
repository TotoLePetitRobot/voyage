from django.shortcuts import redirect


class PseudoRequired(Exception):
    def __init__(self, trip_pk):
        self.trip_pk = trip_pk


class PseudoRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, PseudoRequired):
            return redirect("trip_pseudo", pk=exception.trip_pk)
        return None
