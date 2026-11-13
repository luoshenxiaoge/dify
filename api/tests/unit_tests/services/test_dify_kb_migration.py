import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from opendal.exceptions import NotFound
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from models.dataset import Dataset, Document
from models.model import UploadFile
from services import dify_kb_migration as migration


@pytest.fixture
def document() -> Document:
    return Document(id="doc-test", dataset_id="dataset-test", name="迁移资料.docx", data_source_info="{}")


@pytest.fixture
def segments() -> list[dict[str, object]]:
    return [
        {"source_segment_id": "s1", "position": 1, "content": "第一段中文正文", "is_parent": False},
        {"source_segment_id": "s2", "position": 2, "content": "第二段正文", "is_parent": False},
    ]


@pytest.fixture
def upload_file() -> Mock:
    upload = Mock(spec=UploadFile)
    upload.name = "原始文档.docx"
    upload.key = "uploads/original"
    upload.mime_type = "application/test"
    return upload


@pytest.fixture
def migrator(monkeypatch: pytest.MonkeyPatch) -> migration.DifyKnowledgeMigrator:
    source_storage = Mock()
    source_storage.exists.return_value = True
    monkeypatch.setattr(migration, "storage", source_storage)
    return migration.DifyKnowledgeMigrator(
        Mock(spec=Session),
        Mock(spec=migration.StudioManagerClient),
        migration.MigrationSettings(project_id="project", workspace_id="workspace"),
        tenant_id="tenant",
        strategy="segments",
    )


def test_existing_source_preserves_original_bytes(migrator, document, segments, upload_file):
    document.file_id = "upload-test"
    migrator.db.get.return_value = upload_file
    migration.storage.load_once.return_value = b"original bytes"

    upload, content = migrator._source_file(document, segments)

    assert (upload.name, upload.mime_type, content) == (upload_file.name, upload_file.mime_type, b"original bytes")
    assert not upload.reconstructed_from_segments


@pytest.mark.parametrize("strategy", ["source_file", "segments"])
def test_migration_uses_source_filename_for_upload_and_document_name(
    migrator, document, segments, upload_file, monkeypatch, strategy
):
    migrator.strategy = strategy
    document.name = "加加加"
    document.file_id = "upload-test"
    upload_file.name = "源文件.jpeg"
    upload_file.mime_type = "image/jpeg"
    migrator.db.get.return_value = upload_file
    content = b"\xff\xd8\xfftest"
    migration.storage.load_once.return_value = content
    migrator.db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [document]
    progress = SimpleNamespace(
        status="RUNNING",
        target_knowledge_repo_id="kb-target",
        last_success_document_id=None,
        succeeded_documents=0,
    )
    monkeypatch.setattr(migrator, "_progress", Mock(return_value=progress))
    monkeypatch.setattr(migrator, "_segments", Mock(return_value=segments))

    session = Mock()
    session.post.return_value.ok = True
    session.post.return_value.json.return_value = {"file_id": "target-file"}
    client = migration.StudioManagerClient("http://manager", "key", migration.MigrationSettings("0", "ws"))
    client.session = session
    migrator.client = client

    assert migrator._migrate_dataset(Dataset(id="dataset-test", name="test")) == 1

    files = session.post.call_args.kwargs["files"]
    assert files["file"] == ("源文件.jpeg", content, "image/jpeg")
    if strategy == "source_file":
        assert files["document_name"][1] == "源文件.jpeg"
    else:
        payload = json.loads(files["payload"][1])
        assert payload["document_name"] == "源文件.jpeg"
        assert payload["segments"] == segments


def test_parent_child_configuration_preserves_overlap(migrator):
    rule = SimpleNamespace(
        mode="hierarchical",
        rules_dict={
            "segmentation": {"max_tokens": 50, "chunk_overlap": 50, "separator": "@@"},
            "subchunk_segmentation": {"max_tokens": 4000, "chunk_overlap": 25, "separator": "##"},
        },
    )
    dataset = SimpleNamespace(latest_process_rule=rule, retrieval_model={}, name="test", description="test")
    split = migrator._knowledge_payload(dataset, [])["split_conf"]
    assert split["parent_max_tokens"] == 50
    assert split["child_max_tokens"] == 4000
    assert split["parent_chunk_overlap"] == 50
    assert split["child_chunk_overlap"] == 25


@pytest.mark.parametrize("strategy", ["source_file", "segments"])
@pytest.mark.parametrize(
    ("extract_type", "ocr_enabled"),
    [
        ("default", False),
        ("ocr_parse", True),
        ("enhanced", True),
        ("ocr_parse_all_word", True),
        (None, False),
        ("", False),
    ],
)
def test_migrated_knowledge_maps_source_extract_type(migrator, strategy, extract_type, ocr_enabled):
    migrator.strategy = strategy
    dataset = SimpleNamespace(
        latest_process_rule=None, retrieval_model={}, name="OCR迁移", description="", extract_type=extract_type
    )
    payload = migrator._knowledge_payload(dataset, [])
    assert payload["parse_conf"]["ocr_enabled"] is ocr_enabled
    assert payload["parse_conf"]["image_enabled"] is False


@pytest.mark.parametrize("strategy", ["source_file", "segments"])
def test_legacy_dataset_without_extract_type_uses_normal_parsing(migrator, strategy):
    migrator.strategy = strategy
    dataset = SimpleNamespace(latest_process_rule=None, retrieval_model={}, name="旧知识库", description="")
    payload = migrator._knowledge_payload(dataset, [])
    assert payload["parse_conf"]["ocr_enabled"] is False
    assert payload["parse_conf"]["image_enabled"] is False


@pytest.mark.parametrize(("retrieval", "expected"), [(None, 2), ({}, 2), ({"top_k": 7}, 7)])
def test_migration_preserves_top_k_or_uses_production_dataset_default(migrator, retrieval, expected):
    dataset = SimpleNamespace(latest_process_rule=None, retrieval_model=retrieval, name="test", description="")
    assert migrator._knowledge_payload(dataset, [])["top_k"] == expected


@pytest.mark.parametrize("extract_type", ["default", "ocr_parse", "enhanced", "ocr_parse_all_word", None, ""])
def test_reads_production_extract_type_not_mapped_by_local_orm(migrator, extract_type):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE datasets (id TEXT PRIMARY KEY, extract_type TEXT)"))
        connection.execute(
            text("INSERT INTO datasets (id, extract_type) VALUES (:id, :extract_type)"),
            {"id": "source", "extract_type": extract_type},
        )
    try:
        with Session(engine) as session:
            migrator.db = session
            dataset = SimpleNamespace(
                id="source", latest_process_rule=None, retrieval_model={}, name="test", description=""
            )
            payload = migrator._knowledge_payload(dataset, [], extract_type=migrator._source_extract_type(dataset))
            assert payload["parse_conf"]["ocr_enabled"] is (extract_type not in (None, "", "default"))
    finally:
        engine.dispose()


def test_database_without_extract_type_column_uses_normal_parsing(migrator):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE datasets (id TEXT PRIMARY KEY)"))
    try:
        with Session(engine) as session:
            migrator.db = session
            assert migrator._source_extract_type(SimpleNamespace(id="legacy")) == "default"
    finally:
        engine.dispose()


def test_mapped_extract_type_does_not_require_schema_lookup(migrator):
    assert migrator._source_extract_type(SimpleNamespace(extract_type="enhanced")) == "enhanced"
    migrator.db.connection.assert_not_called()


def test_new_target_knowledge_uses_database_extract_type(migrator, monkeypatch):
    progress = SimpleNamespace(
        status="RUNNING",
        target_knowledge_repo_id=None,
        last_success_document_id=None,
        succeeded_documents=0,
        total_documents=0,
        completed_at=None,
    )
    monkeypatch.setattr(migrator, "_progress", Mock(return_value=progress))
    read_type = Mock(return_value="ocr_parse_all_word")
    monkeypatch.setattr(migrator, "_source_extract_type", read_type)
    migrator.db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
    dataset = SimpleNamespace(id="source", latest_process_rule=None, retrieval_model={}, name="test", description="")

    assert migrator._migrate_dataset(dataset) == 0
    read_type.assert_called_once_with(dataset)
    assert migrator.client.create_knowledge.call_args.args[0]["parse_conf"]["ocr_enabled"] is True


@pytest.mark.parametrize("missing", ["record", "blob", "file_race", "opendal_race"])
def test_missing_source_reconstructs_utf8_txt(migrator, document, segments, missing, upload_file):
    document.file_id = "upload-missing"
    migrator.db.get.return_value = upload_file
    if missing == "record":
        migrator.db.get.return_value = None
    elif missing == "blob":
        migration.storage.exists.return_value = False
    else:
        migration.storage.load_once.side_effect = FileNotFoundError() if missing == "file_race" else NotFound("missing")

    upload, content = migrator._source_file(document, segments)

    assert upload.name == "迁移资料-分段重建.txt"
    assert upload.mime_type == "text/plain"
    assert upload.reconstructed_from_segments
    text = content.decode("utf-8")
    assert "源文件缺失" in text
    assert text.index("第一段中文正文") < text.index("第二段正文")
    assert "s1" in text
    assert "s2" in text
    migration.storage.save.assert_not_called()


def test_reconstructed_txt_retains_questions_answers_and_parent_links(migrator, document):
    migrator.db.get.return_value = None
    chunks = [
        {"source_segment_id": "parent-1", "position": 0, "content": "父块内容", "is_parent": True},
        {"source_segment_id": "child-1", "position": 0, "content": "子块内容", "parent_source_segment_id": "parent-1"},
        {"source_segment_id": "qa-1", "position": 1, "content": "如何迁移？", "answer": "通过分段直导。"},
    ]

    _, content = migrator._source_file(document, chunks)

    text = content.decode("utf-8")
    assert "父分段" in text
    assert "子分段" in text
    assert "父分段ID：parent-1" in text
    assert "问题：如何迁移？" in text
    assert "答案：通过分段直导。" in text
    assert text.index("父块内容") < text.index("子块内容") < text.index("如何迁移？")


@pytest.mark.parametrize("chunks", [[], [{"content": "   "}]])
def test_missing_source_without_usable_segments_fails(migrator, document, chunks):
    migrator.db.get.return_value = None

    with pytest.raises(migration.MigrationError, match="没有可用分段"):
        migrator._source_file(document, chunks)


@pytest.mark.parametrize("error", [PermissionError("denied"), TimeoutError("storage timeout")])
@pytest.mark.parametrize("operation", ["exists", "load_once"])
def test_storage_errors_are_not_treated_as_missing(migrator, document, segments, error, upload_file, operation):
    document.file_id = "upload-test"
    migrator.db.get.return_value = upload_file
    getattr(migration.storage, operation).side_effect = error

    with pytest.raises(type(error)):
        migrator._source_file(document, segments)


@pytest.mark.parametrize("missing_record", [True, False])
def test_source_file_strategy_keeps_missing_file_failure(migrator, document, missing_record, upload_file):
    migrator.strategy = "source_file"
    document.file_id = "upload-missing"
    migrator.db.get.return_value = None if missing_record else upload_file
    migration.storage.load_once.side_effect = FileNotFoundError("File not found")

    with pytest.raises((migration.MigrationError, FileNotFoundError)):
        migrator._source_file(document)


def test_segments_submission_keeps_original_segments_checksum_and_identity(migrator, document, segments, monkeypatch):
    progress = SimpleNamespace(
        status="RUNNING",
        target_knowledge_repo_id="kb-target",
        last_success_document_id=None,
        succeeded_documents=0,
        total_documents=0,
        completed_at=None,
    )
    monkeypatch.setattr(migrator, "_progress", Mock(return_value=progress))
    read_segments = Mock(return_value=segments)
    monkeypatch.setattr(migrator, "_segments", read_segments)
    migrator.db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [document]
    migrator.db.get.return_value = None
    migrator.client.import_document.return_value = {"file_id": "target-doc"}

    assert migrator._migrate_dataset(Dataset(id="dataset-test", name="test")) == 1

    args = migrator.client.import_document.call_args.args
    payload, filename, content, mime_type = args[1:]
    assert payload["strategy"] == "segments"
    assert payload["segments"] is segments
    assert payload["content_sha256"] == hashlib.sha256(content).hexdigest()
    identity = "dify-1.9.1:dataset-test:doc-test:segments"
    assert payload["idempotency_key"] == hashlib.sha256(identity.encode()).hexdigest()
    assert payload["source"]["source_file_reconstructed"] is True
    assert filename.endswith("-分段重建.txt")
    assert payload["document_name"] == payload["source_file_name"] == filename
    assert mime_type == "text/plain"
    assert progress.last_success_document_id == document.id
    assert progress.status == "SUCCEEDED"
    filters = migrator.db.query.return_value.filter.call_args.args
    assert any(getattr(condition.left, "name", None) == "enabled" for condition in filters)
    read_segments.assert_called_once_with(document)


def test_migration_uses_dataset_models_and_default_qa_model(caplog):
    from core.model_runtime.entities.model_entities import ModelType
    from services.dify_kb_migration import DifyKnowledgeMigrator

    model_manager = Mock()
    model_manager.get_model_instance.side_effect = lambda **kwargs: SimpleNamespace(
        provider=kwargs["provider"],
        model=kwargs["model"],
        invoke_text_embedding=Mock(return_value=SimpleNamespace(embeddings=[[0.1, 0.2, 0.3]])),
        credentials={
            "endpoint_url": "https://models.example/v1",
            "api_key": "test-key",
            "endpoint_model_name": "upstream-" + kwargs["model"],
        },
    )
    model_manager.get_default_model_instance.return_value = SimpleNamespace(
        provider="langgenius/openai_api_compatible/openai_api_compatible",
        model="default-llm",
        credentials={
            "endpoint_url": "https://models.example/v1",
            "api_key": "test-key",
            "endpoint_model_name": "upstream-llm",
        },
    )
    dataset = SimpleNamespace(
        id="dataset-test",
        embedding_model_provider="custom/provider",
        embedding_model="selected-embedding",
        retrieval_model={
            "reranking_enable": False,
            "reranking_model": {
                "reranking_provider_name": "custom/provider",
                "reranking_model_name": "selected-rerank",
            },
        },
    )
    client = Mock()
    client.ensure_model.side_effect = lambda model: {"id": model["model_name"] + "-id", "name": model["model_name"]}
    events = Mock()
    migrator = DifyKnowledgeMigrator(
        Mock(), client, migration.MigrationSettings("0", "workspace"), "tenant", "source_file", on_event=events
    )
    payload = {"split_conf": {"qa_enabled": True}, "rerank_enabled": False}
    with caplog.at_level("INFO", logger=migration.__name__):
        migrator._attach_source_models(dataset, payload, model_manager=model_manager)
        dataset.id = "next-dataset"
        migrator._attach_source_models(dataset, payload, model_manager=model_manager)
        dataset.id = "different-provider"
        dataset.embedding_model_provider = "different/provider"
        migrator._attach_source_models(dataset, payload, model_manager=model_manager)

    assert payload["embedding_model"]["id"] == "selected-embedding-id"
    assert payload["rerank_model"]["id"] == "selected-rerank-id"
    assert payload["rerank_enabled"] is False
    assert payload["split_conf"]["qa_model"] == "default-llm"
    model_manager.get_default_model_instance.assert_called_once_with("tenant", ModelType.LLM)
    assert [call.args[0]["api_url"] for call in client.ensure_model.call_args_list] == [
        "https://models.example/v1/embeddings",
        "https://models.example/v1/rerank",
        "https://models.example/v1/chat/completions",
        "https://models.example/v1/embeddings",
    ]
    models = [
        {"model_type": "text-embedding", "model_provider": "custom/provider", "model_name": "selected-embedding"},
        {"model_type": "rerank", "model_provider": "custom/provider", "model_name": "selected-rerank"},
        {
            "model_type": "llm",
            "model_provider": "langgenius/openai_api_compatible/openai_api_compatible",
            "model_name": "default-llm",
        },
    ]
    assert events.call_args_list == [
        *[call("model_cache_miss", "dataset-test", model) for model in models],
        *[call("model_cache_hit", "next-dataset", model) for model in models],
        call("model_cache_miss", "different-provider", {**models[0], "model_provider": "different/provider"}),
        *[call("model_cache_hit", "different-provider", model) for model in models[1:]],
    ]
    cached_log = (
        "dataset_id=next-dataset model_type=text-embedding provider=custom/provider model=selected-embedding cache=hit"
    )
    assert cached_log in caplog.text
    assert "test-key" not in caplog.text


def test_model_failure_logs_traceback_without_credentials(migrator, caplog):
    from core.model_runtime.entities.model_entities import ModelType
    from core.model_runtime.errors.invoke import InvokeBadRequestError

    def invoke_embedding(*args, **kwargs):
        try:
            raise ValueError("upstream response with secret-test-token")
        except ValueError as exc:
            raise InvokeBadRequestError(
                "400 Client Error for url: https://models.example/?key=secret-test-token"
            ) from exc

    model = "Bge-m3-test"
    provider = "langgenius/openai_api_compatible/openai_api_compatible"
    manager = Mock()
    manager.get_model_instance.return_value = SimpleNamespace(
        provider=provider,
        model=model,
        credentials={"endpoint_url": "https://models.example/v1", "api_key": "secret-test-token"},
        invoke_text_embedding=invoke_embedding,
    )
    migrator.on_event = Mock()
    dataset = Dataset(id="dataset-test")

    with pytest.raises(migration.MigrationError, match="读取 Dify text-embedding 模型 Bge-m3-test 失败") as raised:
        migrator._ensure_source_model(dataset, manager, ModelType.TEXT_EMBEDDING, provider, model)

    assert "Traceback (most recent call last)" in caplog.text
    assert "invoke_embedding" in caplog.text
    assert "InvokeBadRequestError (HTTP 400)" in caplog.text
    assert "ValueError" in caplog.text
    assert "secret-test-token" not in caplog.text
    assert "secret-test-token" not in str(raised.value)
    migrator.on_event.assert_called_once_with(
        "model_cache_miss",
        dataset.id,
        {"model_type": "text-embedding", "model_provider": provider, "model_name": model},
    )
