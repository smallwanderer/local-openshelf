from .models import UserStorage

def storage_usage(request):
    """
    현재 워크스페이스의 저장 공간 정보를 템플릿에 제공합니다.
    """
    if request.user.is_authenticated and getattr(request, "workspace", None) is not None:
        storage, _ = UserStorage.objects.get_or_create(
            workspace=request.workspace,
            defaults={"user": request.workspace.created_by},
        )
        return {
            'user_storage': storage
        }
    return {}
