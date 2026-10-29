import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opendal.exceptions import NotFound
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
    assert mime_type == "text/plain"
    assert progress.last_success_document_id == document.id
    assert progress.status == "SUCCEEDED"
    read_segments.assert_called_once_with(document)
