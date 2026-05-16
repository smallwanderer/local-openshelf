from rest_framework import serializers

from document_ai.models import SearchJob

class VectorSearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(
        required=True, 
        help_text="검색할 질문이나 키워드"
    )
    top_k = serializers.IntegerField(
        default=5, 
        min_value=1, 
        max_value=50, 
        help_text="반환할 최대 결과 수"
    )
    threshold = serializers.FloatField(
        required=False, 
        help_text="허용할 최대 유사도 거리(값이 작을수록 유사함). 예: 0.8"
    )
    node_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="특정 파일(Node) 내에서만 검색할 경우 ID 리스트"
    )

class EvidenceSerializer(serializers.Serializer):
    chunk_id = serializers.IntegerField()
    text = serializers.CharField()
    context_text = serializers.CharField()
    section = serializers.CharField(allow_blank=True)
    pages = serializers.CharField()
    distance = serializers.FloatField()

class VectorSearchResponseSerializer(serializers.Serializer):
    node_id = serializers.UUIDField()
    node_name = serializers.CharField()
    file_ext = serializers.CharField()
    doc_score = serializers.FloatField()
    evidences = serializers.ListField(child=EvidenceSerializer())


class SearchJobCreateResponseSerializer(serializers.Serializer):
    job_id = serializers.IntegerField()
    status = serializers.CharField()
    poll_url = serializers.CharField()


class SearchJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = SearchJob
        fields = [
            "id",
            "query",
            "top_k",
            "threshold",
            "node_ids",
            "status",
            "results",
            "error_message",
            "created_at",
            "started_at",
            "completed_at",
            "updated_at",
        ]
