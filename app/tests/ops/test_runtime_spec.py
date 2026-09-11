import pytest

from llm_installation.runtime_lifecycle import build_runtime_spec, make_generation_id

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "runtime,image",
    [("llama.cpp", "dotori/llama-rag"), ("vllm", "dotori/vllm-rag")],
)
@pytest.mark.parametrize(
    "scope,container_name,network_name",
    [
        ("production", "dotori-llm", "dotori-runtime"),
    ],
)
def test_build_runtime_spec_derives_fields(tmp_path, runtime, image, scope, container_name, network_name):
    spec = build_runtime_spec(scope, runtime, "some-model", "20240101-abc123", repo_root=tmp_path)

    assert spec.scope == scope
    assert spec.runtime == runtime
    assert spec.model_id == "some-model"
    assert spec.image == image
    assert spec.container_name == container_name
    assert spec.network_name == network_name
    assert spec.network_alias == "rag-runtime"
    assert spec.args_file == (
        tmp_path
        / "data"
        / "config"
        / "runtime_scopes"
        / scope
        / "generations"
        / "20240101-abc123"
        / "runtime.args"
    )


@pytest.mark.parametrize("scope", ["development", "staging"])
def test_build_runtime_spec_rejects_unknown_scope(tmp_path, scope):
    with pytest.raises(ValueError):
        build_runtime_spec(scope, "llama.cpp", "m", "g1", repo_root=tmp_path)


def test_build_runtime_spec_rejects_unknown_runtime(tmp_path):
    with pytest.raises(ValueError):
        build_runtime_spec("production", "mystery-engine", "m", "g1", repo_root=tmp_path)


def test_make_generation_id_is_sortable_and_content_addressed():
    a = make_generation_id("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    b = make_generation_id("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    assert a != b
    assert a.split("-", 1)[1] == "aaaaaaaaaaaa"
    assert b.split("-", 1)[1] == "bbbbbbbbbbbb"
    assert a.split("-", 1)[0].isdigit()
