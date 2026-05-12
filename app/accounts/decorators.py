from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


def email_verification_required(function):
    @wraps(function)
    def wrap(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")

        if request.user.email_verified:
            return function(request, *args, **kwargs)

        messages.warning(request, "Email verification is required to use this service.")
        return redirect("accounts:verification_required")

    return wrap
