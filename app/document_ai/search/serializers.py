from rest_framework import serializers
from django.conf import settings

class VectorSearchRequestSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(
        choices=["basic", "advanced"],
        default="advanced",
        help_text="두 값 모두 입력 질의를 직접 검색합니다 (advanced의 query-understanding 파이프라인은 평가 후 폐기됨).",
    )
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
    compressed_text = serializers.CharField(required=False, allow_blank=True)
    compression = serializers.DictField(required=False)
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
    compressed_text = serializers.CharField(required=False, allow_blank=True)
    compression = serializers.DictField(required=False)
    score_details = ScoreDetailsSerializer(required=False)
    evidences = serializers.ListField(child=EvidenceSerializer())


class SynchronousSearchResponseSerializer(serializers.Serializer):
    results = VectorSearchResponseSerializer(many=True)
    performance_metrics = serializers.DictField()
    query_plan = serializers.DictField(required=False)


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


class RAGRequestSerializer(serializers.Serializer):
    question = serializers.CharField(required=True)
    top_k = serializers.IntegerField(default=getattr(settings, "RAG_SEARCH_TOP_K", 3), min_value=1, max_value=10)
    threshold = serializers.FloatField(required=False, min_value=0.0)
    language = serializers.ChoiceField(choices=["ko", "en"], default="ko")
    node_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="특정 파일 또는 폴더(Node) 범위에서만 RAG 답변을 생성할 경우 ID 리스트",
    )
