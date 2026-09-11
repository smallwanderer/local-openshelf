from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model, login

from .services import get_or_create_default_local_admin


class LocalProfileMiddleware:
    """When LOGIN_REQUIRED is off, auto-authenticate anonymous requests as a local
    profile (no password) instead of asking the user to sign in. Must run after
    AuthenticationMiddleware, which resolves request.user first."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)

        self._prepare_local_profile(request)
        return self.get_response(request)

    async def __acall__(self, request):
        # Session/authentication writes use Django's synchronous ORM and session
        # backend. Keep that short section in Django's thread-sensitive lane;
        # the downstream async response and its stream stay on the event loop.
        await sync_to_async(self._prepare_local_profile, thread_sensitive=True)(request)
        return await self.get_response(request)

    def _prepare_local_profile(self, request):
        # Internal service-to-service calls (embedding-executor query embedding
        # proxy, etc.) have no browser session and shouldn't provision/log in
        # a local profile on every call.
        if "/internal/" in request.path:
            return
        if not settings.LOGIN_REQUIRED and not request.user.is_authenticated:
            user = self._resolve_active_profile(request)
            user.backend = "django.contrib.auth.backends.ModelBackend"
            login(request, user)
            request.session["active_profile_id"] = user.id

    def _resolve_active_profile(self, request):
        User = get_user_model()
        profile_id = request.session.get("active_profile_id")
        if profile_id:
            try:
                return User.objects.get(pk=profile_id, is_active=True)
            except User.DoesNotExist:
                pass
        return get_or_create_default_local_admin()
