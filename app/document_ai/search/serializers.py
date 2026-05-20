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
        help_text="검색 임계값. inner_product에서는 최소 dot similarity, cosine/l2에서는 최대 distance. 예: 0.8"
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
    dense_score = serializers.FloatField(required=False)
    sparse_score = serializers.FloatField(required=False)
    hybrid_score = serializers.FloatField(required=False)
    candidate_dense_norm = serializers.FloatField(required=False)
    score_checks = serializers.ListField(
        child=serializers.CharField(),
        required=False,
    )


class ScoreDetailsSerializer(serializers.Serializer):
    distance_strategy = serializers.CharField()
    pooling_method = serializers.CharField()
    dense_weight = serializers.FloatField()
    sparse_weight = serializers.FloatField()
    query_dense_norm = serializers.FloatField()
    query_sparse_terms = serializers.IntegerField()
    query_sparse_norm = serializers.FloatField()
    input_threshold = serializers.FloatField(required=False, allow_null=True)
    distance_threshold = serializers.FloatField(required=False, allow_null=True)
    hit_count = serializers.IntegerField()
    pool_hit_count = serializers.IntegerField()
    evidence_hit_count = serializers.IntegerField()
    pool_tau = serializers.FloatField()
    pool_top_k = serializers.IntegerField()
    pooled_score = serializers.FloatField(required=False)
    softmax_score = serializers.FloatField()
    length_penalty = serializers.FloatField()
    doc_score = serializers.FloatField()
    top_hybrid_scores = serializers.ListField(child=serializers.FloatField())
    checks = serializers.ListField(child=serializers.CharField())

class VectorSearchResponseSerializer(serializers.Serializer):
    node_id = serializers.UUIDField()
    node_name = serializers.CharField()
    file_ext = serializers.CharField()
    doc_score = serializers.FloatField()
    score_details = ScoreDetailsSerializer(required=False)
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
            "tuning_params",
            "status",
            "task_id",
            "results",
            "error_message",
            "created_at",
            "started_at",
            "completed_at",
            "updated_at",
        ]

class VectorTuningRequestSerializer(serializers.Serializer):
    query = serializers.CharField(required=True)
    top_k = serializers.IntegerField(default=5, min_value=0)

    # 10 Tuning Parameters
    dense_weight = serializers.FloatField(required=False, min_value=0.0, max_value=1.0)
    sparse_weight = serializers.FloatField(required=False, min_value=0.0, max_value=1.0)
    candidate_multiplier = serializers.IntegerField(required=False, min_value=1)
    per_node_candidate_cap = serializers.IntegerField(required=False, min_value=0)
    query_sparse_top_n = serializers.IntegerField(required=False, min_value=1)
    evidence_top_k = serializers.IntegerField(required=False, min_value=1)
    pool_top_k = serializers.IntegerField(required=False, min_value=1)
    pool_tau = serializers.FloatField(required=False, min_value=0.1)
    doc_length_penalty_alpha = serializers.FloatField(required=False, min_value=0.0)
    evidence_context_window = serializers.IntegerField(required=False, min_value=0)
