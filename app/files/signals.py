from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.conf import settings
from .models import FileBlob, UserStorage
from django.db.models import F

@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_storage(sender, instance, created, **kwargs):
    """
    유저가 새롭게 생성될 때 전용 UserStorage(할당량 관리용) 레코드를 자동 생성합니다.
    """
    if created:
        UserStorage.objects.get_or_create(user=instance)

@receiver(post_save, sender=FileBlob)
def update_used_size_on_save(sender, instance, created, **kwargs):
    """
    FileBlob이 새로 생성될 때(파일 업로드) 해당 공유 스토리지 사용량을 증가시킵니다.
    """
    if created and instance.size:
        UserStorage.objects.filter(user=instance.node.owner).update(
            used_size=F('used_size') + instance.size
        )

@receiver(post_delete, sender=FileBlob)
def update_used_size_on_delete(sender, instance, **kwargs):
    """
    FileBlob이 삭제될 때 해당 공유 스토리지 사용량을 차감합니다.
    """
    if instance.size:
        # 이미 0보다 작아지는 것을 방지하기 위해 filter 사용
        UserStorage.objects.filter(user=instance.node.owner).update(
            used_size=F('used_size') - instance.size
        )
