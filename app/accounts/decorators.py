from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from .api_responses import api_error_response, is_json_api_request


def api_login_required(function):
    @wraps(function)
    def wrap(request, *args, **kwargs):
        if request.user.is_authenticated:
            return function(request, *args, **kwargs)
        return api_error_response(
            "AUTHENTICATION_REQUIRED",
            "Authentication is required.",
            status=401,
        )

    return wrap


def operator_api_required(function):
    """Require a verified staff session and keep API failures JSON-only."""

    @wraps(function)
    def wrap(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return api_error_response(
                "AUTHENTICATION_REQUIRED",
                "Authentication is required.",
                status=401,
            )
        if not user.email_verified:
            return api_error_response(
                "EMAIL_VERIFICATION_REQUIRED",
                "Email verification is required.",
                status=403,
            )
        if not user.is_staff:
            return api_error_response(
                "PERMISSION_DENIED",
                "Operator access is required.",
                status=403,
            )
        return function(request, *args, **kwargs)

    return wrap


def email_verification_required(function):
    @wraps(function)
    def wrap(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if is_json_api_request(request):
                return api_error_response(
                    "AUTHENTICATION_REQUIRED",
                    "Authentication is required.",
                    status=401,
                )
            return redirect("accounts:login")

        if request.user.email_verified:
            return function(request, *args, **kwargs)

        if is_json_api_request(request):
            return api_error_response(
                "EMAIL_VERIFICATION_REQUIRED",
                "Email verification is required.",
                status=403,
            )

        messages.warning(request, "Email verification is required to use this service.")
        return redirect("accounts:verification_required")

    return wrap
