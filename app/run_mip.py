import os
import sys
from pathlib import Path
import django

# Setup Django environment
current_dir = Path(__file__).resolve().parent
sys.path.append(str(current_dir))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# Docker 밖(로컬)에서 실행할 때 컨테이너 내부 호스트명('db')을 찾지 못하는 문제를 방지하기 위해 로컬 호스트로 덮어씁니다.
os.environ["POSTGRES_HOST"] = "127.0.0.1"
os.environ["POSTGRES_PORT"] = "5433"
django.setup()

# 이제 Django가 세팅되었으므로, 모델들을 안전하게 임포트할 수 있습니다.
from document_ai.models import ChunkEmbedding
from pgvector.django import MaxInnerProduct

print("Fetching an embedding from the database...")
embedding_obj = ChunkEmbedding.objects.exclude(vector__isnull=True).first()

if not embedding_obj:
    print("No embedding found in the database.")
else:
    print(f"Found ChunkEmbedding ID: {embedding_obj.id}")
    vector = embedding_obj.vector
    print(f"Embedding Vector (first 5 dims): {vector[:5]}")
    print(f"Embedding Vector length: {len(vector)}")

    print("\nCalculating MaxInnerProduct with other embeddings in the database...")
    similar_chunks = ChunkEmbedding.objects.exclude(
        id=embedding_obj.id
    ).annotate(
        mip=MaxInnerProduct('vector', vector)
    ).order_by('mip')[:5]

    for chunk in similar_chunks:
        real_ip = -chunk.mip if chunk.mip is not None else None
        print(f"Chunk ID: {chunk.id}, MIP Score (Raw): {chunk.mip}, Real Inner Product: {real_ip}")
