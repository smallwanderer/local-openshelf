import os
import django
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from document_ai.search.retriever import VectorRetriever

try:
    retriever = VectorRetriever()
    results = retriever.retrieve(query="테스트", top_k=5)
    print("Results length:", len(results))
    for res in results:
        print("node_id:", res.get("node_id"))
        print("doc_score:", res.get("doc_score"))
        for ev in res.get("evidences", []):
            print("  Evidence distance:", ev.get("distance"))
except Exception as e:
    import traceback
    traceback.print_exc()
