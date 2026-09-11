from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from accounts.decorators import email_verification_required
from files.models import Node, NodeType

@login_required
@email_verification_required
def index(request):
    """ 메인 파일 목록 뷰 (빈 틀 반환) """
    return render(request, "files/file_list.html")

@login_required
@email_verification_required
def node_view(request, uid):
    """ 노드(파일/폴더) 상세 뷰 """
    node = get_object_or_404(Node, uid=uid, workspace=request.workspace)

    # 브레드크럼 빌드
    breadcrumbs = []
    current = node
    while current:
        breadcrumbs.insert(0, {"uid": str(current.uid), "name": current.name})
        current = current.parent

    if node.node_type == NodeType.FOLDER:
        return render(request, "files/file_list.html", {
            "current_folder": node,
            "current_folder_uid": str(node.uid),
            "current_folder_name": node.name,
            "breadcrumbs": breadcrumbs,
        })
    else:
        return render(request, "files/file_detail.html", {
            "file": node,
            "breadcrumbs": breadcrumbs,
        })

@login_required
@email_verification_required
def upload(request):
    """ 파일 업로드 뷰 """
    return render(request, "files/upload.html")

@login_required
@email_verification_required
def recent(request):
    return render(request, "files/recent_files.html")

@login_required
@email_verification_required
def starred(request):
    return render(request, "files/starred_files.html")

@login_required
@email_verification_required
def trash(request):
    return render(request, "files/trash_files.html")


@login_required
@email_verification_required
def rag_workspace(request):
    from document_ai.services.llm_endpoint_service import (
        get_or_create_llm_preference,
        get_effective_rag_target,
    )

    selected_nodes = request.GET.get("nodes", "").strip()
    preference = get_or_create_llm_preference(request.workspace, owner=request.user)
    rag_target = get_effective_rag_target(preference)
    return render(request, "files/rag_workspace.html", {
        "selected_nodes": selected_nodes,
        "rag_model_label": rag_target.get("model", ""),
        "rag_provider_label": rag_target.get("label", "Server default"),
    })

@login_required
@email_verification_required
def ai_search_history(request):
    from document_ai.models import RAGJob
    from django.core.paginator import Paginator

    history_list = RAGJob.objects.filter(
        workspace=request.workspace,
        owner=request.user,
        status="completed"
    ).order_by("-completed_at").select_related("search_job")

    paginator = Paginator(history_list, 10)  # Show 10 per page
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(request, "files/ai_search_history.html", {
        "page_obj": page_obj,
    })
