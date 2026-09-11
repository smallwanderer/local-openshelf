from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async

from .models import Workspace, WorkspaceMembership


class ActiveWorkspaceMiddleware:
    """Resolve request.workspace / request.workspace_membership for the signed-in
    user's active workspace, falling back to their personal workspace. Must run
    after AuthenticationMiddleware and LocalProfileMiddleware, both of which
    resolve request.user first."""

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

        self._resolve_active_workspace(request)
        return self.get_response(request)

    async def __acall__(self, request):
        await sync_to_async(self._resolve_active_workspace, thread_sensitive=True)(request)
        return await self.get_response(request)

    def _resolve_active_workspace(self, request):
        request.workspace = None
        request.workspace_membership = None

        # Internal service-to-service calls have no session/workspace context.
        if "/internal/" in request.path:
            return
        if not request.user.is_authenticated:
            return

        membership = self._membership_from_session(request) or self._fallback_personal_membership(request)
        if membership is not None:
            request.workspace = membership.workspace
            request.workspace_membership = membership

    def _membership_from_session(self, request):
        workspace_id = request.session.get("active_workspace_id")
        if not workspace_id:
            return None
        return (
            WorkspaceMembership.objects.filter(
                workspace_id=workspace_id,
                user=request.user,
                status=WorkspaceMembership.STATUS_ACTIVE,
            )
            .select_related("workspace")
            .first()
        )

    def _fallback_personal_membership(self, request):
        membership = (
            WorkspaceMembership.objects.filter(
                user=request.user,
                status=WorkspaceMembership.STATUS_ACTIVE,
                workspace__kind=Workspace.KIND_PERSONAL,
            )
            .select_related("workspace")
            .first()
        )
        if membership is not None:
            request.session["active_workspace_id"] = membership.workspace_id
        return membership
