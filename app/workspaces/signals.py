from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Workspace, WorkspaceMembership


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_personal_workspace(sender, instance, created, **kwargs):
    """계정이 생성될 때 전용 개인 워크스페이스와 관리자 멤버십을 자동 생성합니다."""
    if not created:
        return
    workspace = Workspace.objects.create(
        name=f"{instance.display_label}'s Workspace",
        kind=Workspace.KIND_PERSONAL,
        created_by=instance,
    )
    WorkspaceMembership.objects.create(
        workspace=workspace,
        user=instance,
        role=WorkspaceMembership.ROLE_ADMIN,
        status=WorkspaceMembership.STATUS_ACTIVE,
    )
