from .models import UserStorage

def storage_usage(request):
    """
    모든 템플릿에서 로그인한 사용자의 저장 공간 정보(UserStorage)에 접근할 수 있도록 하는 컨텍스트 프로세서입니다.
    """
    if request.user.is_authenticated:
        # get_or_create로 아직 storage 레코드가 없는 경우 생성해서 반환
        storage, _ = UserStorage.objects.get_or_create(user=request.user)
        return {
            'user_storage': storage
        }
    return {}
