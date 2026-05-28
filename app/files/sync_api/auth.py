"""API Token authentication for the Sync API.

Validates ``Authorization: Bearer <key>`` headers against the APIToken model.
"""

import logging
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone

from accounts.models import APIToken

logger = logging.getLogger(__name__)


def api_token_required(view_func):
    """Decorator that authenticates requests via Bearer token.

    On success, ``request.user`` is set to the token owner and
    ``request.api_token`` is set to the APIToken instance.
    """

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return JsonResponse(
                {"ok": False, "errors": ["Missing or invalid Authorization header."]},
                status=401,
            )

        key = auth_header[7:].strip()
        if not key:
            return JsonResponse(
                {"ok": False, "errors": ["Empty API token."]},
                status=401,
            )

        try:
            token = APIToken.objects.select_related("user").get(key=key, is_active=True)
        except APIToken.DoesNotExist:
            return JsonResponse(
                {"ok": False, "errors": ["Invalid or inactive API token."]},
                status=401,
            )

        if not token.user.is_active:
            return JsonResponse(
                {"ok": False, "errors": ["User account is inactive."]},
                status=403,
            )

        # Update last_used_at (fire-and-forget)
        APIToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        request.user = token.user
        request.api_token = token
        return view_func(request, *args, **kwargs)

    return wrapper
