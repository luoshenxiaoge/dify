"""Dify 1.9.1 到 Studio 知识库的迁移器。

迁移器只通过 Studio Manager 对外 API 写入目标系统。source_file 策略上传原文件，
segments 策略上传原文件及 Dify 已完成分段；两条链路共享进度表但互不混用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import requests
from sqlalchemy.orm import Session

from extensions.ext_storage import storage
from models.dataset import ChildChunk, Dataset, Document, DocumentSegment
from models.dataset_migration import DatasetMigrationProgress
from models.model import UploadFile


logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """目标 API 或源数据不满足迁移约束。"""


@dataclass(frozen=True)
class MigrationSettings:
    project_id: str
    workspace_id: str
    instance_id: str = "dify-1.9.1"

    @classmethod
    def from_options(
        cls,
        project_id: str,
        workspace_id: str,
        instance_id: str,
    ) -> "MigrationSettings":
        """从本次 CLI 显式参数构造目标 Studio 迁移范围。

        模型配置不由 Dify 传入。Studio Manager 会在目标 project/workspace
        内选择当前可用的 embedding、rerank、Q&A 模型，并向知识库服务同步
        完整的调用配置，避免 Dify 保存或暴露 Studio 模型标识。
        """
        if not project_id or not workspace_id:
            raise MigrationError("迁移参数缺少 --project-id 或 --workspace-id")
        return cls(
            project_id=str(project_id),
            workspace_id=str(workspace_id),
            instance_id=str(instance_id or "dify-1.9.1"),
        )


class StudioManagerClient:
    """迁移期间唯一允许的 Studio 写入客户端。"""

    def __init__(self, base_url: str, api_key: str, settings: MigrationSettings):
        if not base_url or not api_key:
            raise MigrationError("Studio Manager 地址和知识库 API Key 必须由本次命令显式传入")
        self.base_url = base_url.rstrip("/")
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/openapi/v1/{self.settings.project_id}/agent-manager{suffix}"

    @staticmethod
    def _data(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise MigrationError(f"Studio Manager 返回非 JSON 响应：HTTP {response.status_code}") from exc
        if not response.ok:
            raise MigrationError(f"Studio Manager 请求失败：HTTP {response.status_code}，{body}")
        return body.get("data", body)

    def create_knowledge(self, payload: dict[str, Any]) -> str:
        response = self.session.post(
            self._url(f"/knowledges?workspace_id={self.settings.workspace_id}"), json=payload, timeout=60,
        )
        data = self._data(response)
        for key in ("knowledge_repo_id", "knowledgeRepoId", "id"):
            if data.get(key):
                return str(data[key])
        raise MigrationError(f"创建知识库响应缺少 knowledge_repo_id：{data}")

    def import_document(self, knowledge_repo_id: str, payload: dict[str, Any], file_name: str,
                        file_bytes: bytes, mime_type: str) -> dict[str, Any]:
        suffix = f"/knowledges/{knowledge_repo_id}/migrations/dify/documents:"
        strategy = payload["strategy"]
        endpoint = "upload" if strategy == "source_file" else "import"
        files: dict[str, Any] = {"file": (file_name, file_bytes, mime_type or "application/octet-stream")}
        if strategy == "segments":
            files["payload"] = (None, json.dumps(payload, ensure_ascii=False), "application/json")
        response = self.session.post(
            self._url(f"{suffix}{endpoint}?workspace_id={self.settings.workspace_id}"), files=files, timeout=300,
        )
        return self._data(response)


class DifyKnowledgeMigrator:
    """按 tenant 过滤源数据并以文档粒度推进进度。"""

    def __init__(self, session: Session, client: StudioManagerClient, settings: MigrationSettings,
                 tenant_id: str, strategy: str, force: bool = False):
        if strategy not in {"source_file", "segments"}:
            raise MigrationError("strategy 只能是 source_file 或 segments")
        self.db = session
        self.client = client
        self.settings = settings
        self.tenant_id = tenant_id
        self.strategy = strategy
        self.force = force
        self.fingerprint = hashlib.sha256(
            f"{strategy}|{settings.project_id}|{settings.workspace_id}|{settings.instance_id}".encode()
        ).hexdigest()

    def migrate(self, dataset_ids: Iterable[str] | None = None,
                excluded_dataset_ids: Iterable[str] | None = None) -> dict[str, int]:
        requested = set(dataset_ids or [])
        excluded = set(excluded_dataset_ids or [])
        query = self.db.query(Dataset).filter(
            Dataset.tenant_id == self.tenant_id,
            Dataset.indexing_technique == "high_quality",
        ).order_by(Dataset.created_at, Dataset.id)
        datasets = [item for item in query if (not requested or item.id in requested) and item.id not in excluded]
        result = {"datasets": 0, "succeeded": 0, "skipped": 0, "failed": 0, "documents": 0}
        logger.info(
            "Dify 知识库迁移开始：strategy=%s tenant_id=%s candidates=%d specified_datasets=%d excluded_datasets=%d",
            self.strategy, self.tenant_id, len(datasets), len(requested), len(excluded),
        )
        for dataset_index, dataset in enumerate(datasets, start=1):
            result["datasets"] += 1
            try:
                logger.info(
                    "[知识库 %d/%d] 开始：name=%s dataset_id=%s strategy=%s",
                    dataset_index, len(datasets), dataset.name, dataset.id, self.strategy,
                )
                imported_documents = self._migrate_dataset(dataset)
                if imported_documents is None:
                    result["skipped"] += 1
                    continue
                result["documents"] += imported_documents
                result["succeeded"] += 1
                logger.info(
                    "[知识库 %d/%d] 完成：name=%s imported_documents=%d",
                    dataset_index, len(datasets), dataset.name, imported_documents,
                )
            except Exception as exc:
                result["failed"] += 1
                logger.exception(
                    "[知识库 %d/%d] 失败：name=%s dataset_id=%s error=%s",
                    dataset_index, len(datasets), dataset.name, dataset.id, exc,
                )
                self._mark_dataset_failed(dataset, str(exc))
        logger.info("Dify 知识库迁移结束：%s", result)
        return result

    def _progress(self, dataset: Dataset) -> DatasetMigrationProgress:
        progress = self.db.query(DatasetMigrationProgress).filter_by(
            tenant_id=self.tenant_id, dataset_id=dataset.id, strategy=self.strategy,
            project_id=self.settings.project_id, workspace_id=self.settings.workspace_id,
        ).one_or_none()
        if progress is None:
            progress = DatasetMigrationProgress(
                tenant_id=self.tenant_id, dataset_id=dataset.id, strategy=self.strategy,
                project_id=self.settings.project_id, workspace_id=self.settings.workspace_id,
                parameter_fingerprint=self.fingerprint, status="PENDING",
            )
            self.db.add(progress)
            self.db.flush()
            logger.info("创建迁移进度：dataset_id=%s strategy=%s", dataset.id, self.strategy)
        if progress.status == "SUCCEEDED" and not self.force:
            logger.info(
                "跳过已成功知识库：name=%s dataset_id=%s target_knowledge_repo_id=%s",
                dataset.name, dataset.id, progress.target_knowledge_repo_id,
            )
            return progress
        if progress.parameter_fingerprint != self.fingerprint and not self.force:
            raise MigrationError("迁移参数已变化；请显式使用 --force 重新迁移")
        previous_status = progress.status
        progress.parameter_fingerprint = self.fingerprint
        progress.status = "RUNNING"
        progress.error_message = None
        self.db.commit()
        logger.info(
            "领取迁移进度：name=%s dataset_id=%s previous_status=%s last_success_document_id=%s",
            dataset.name, dataset.id, previous_status, progress.last_success_document_id,
        )
        return progress

    @staticmethod
    def _preprocessing(rules: dict[str, Any]) -> tuple[bool, bool]:
        enabled = {item.get("id"): bool(item.get("enabled")) for item in rules.get("pre_processing_rules", [])}
        return enabled.get("remove_extra_spaces", True), enabled.get("remove_urls_emails", False)

    def _studio_display_name(self, dataset: Dataset) -> str:
        """将 Dify 名称适配为 Studio OpenAPI 的 50 字符受限名称。

        Studio 只接受中英文、数字、连字符及下划线，而 Dify 允许点号、空格和
        括号等字符。只保留经过合法字符清洗后的 Dify 原名称，不附加迁移策略
        或源知识库 ID。
        """
        normalized = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5_-]", "", dataset.name or "").lstrip("-_")
        if not normalized:
            normalized = "difyknowledge"
        return normalized[:50]

    def _knowledge_payload(self, dataset: Dataset, documents: list[Document]) -> dict[str, Any]:
        process_rule = dataset.latest_process_rule
        mode = process_rule.mode if process_rule else "automatic"
        rules = process_rule.rules_dict if process_rule and process_rule.rules_dict else {}
        remove_spaces, remove_urls = self._preprocessing(rules)
        segmentation = rules.get("segmentation") or {"separator": "\n\n", "max_tokens": 500, "chunk_overlap": 50}
        separator = segmentation.get("separator", segmentation.get("delimiter", "\n\n"))
        qa_documents = [document for document in documents if document.doc_form == "qa_model"]
        if qa_documents:
            if self.strategy == "source_file" and len(qa_documents) != len(documents):
                raise MigrationError("源文件策略不支持 Q&A 文档与普通文档混用；请使用分段直导")
            languages = {str(getattr(document, "doc_language", "") or "English") for document in qa_documents}
            if self.strategy == "source_file" and len(languages) > 1:
                raise MigrationError("源文件策略不支持多种 Q&A 语言混用；请使用分段直导")
        split_conf: dict[str, Any]
        if mode == "hierarchical":
            child = rules.get("subchunk_segmentation") or {"separator": "\n", "max_tokens": 512}
            split_conf = {
                "split_mode": "PARENT_CHILD", "dify_compatible": True,
                "parent_mode": rules.get("parent_mode", "paragraph"),
                "parent_max_tokens": int(segmentation.get("max_tokens", 1024)),
                "parent_delimiter": separator,
                "child_max_tokens": int(child.get("max_tokens", 512)),
                "child_delimiter": child.get("separator", child.get("delimiter", "\n")),
                "remove_extra_spaces": remove_spaces, "remove_urls_emails": remove_urls,
            }
        else:
            split_conf = {
                "split_mode": "GENERAL", "dify_compatible": True,
                "separator": separator, "max_tokens": int(segmentation.get("max_tokens", 500)),
                "chunk_overlap": int(segmentation.get("chunk_overlap", 50)),
                "remove_extra_spaces": remove_spaces, "remove_urls_emails": remove_urls,
            }
        if qa_documents and self.strategy == "source_file":
            split_conf = {
                "split_mode": "GENERAL", "dify_compatible": True,
                "separator": separator, "max_tokens": int(segmentation.get("max_tokens", 500)),
                "chunk_overlap": int(segmentation.get("chunk_overlap", 50)),
                "remove_extra_spaces": remove_spaces, "remove_urls_emails": remove_urls,
                "qa_enabled": True, "qa_language": next(iter(languages)),
            }
        retrieval = dataset.retrieval_model or {}
        # Dify 的检索枚举与 Studio 的对外枚举并不完全同名，不能直接移除
        # "_search" 后缀，否则 semantic_search 会得到 Studio 不支持的 semantic。
        retrieval_mode = {
            "semantic_search": "vector",
            "full_text_search": "keyword",
            "hybrid_search": "hybrid",
            "vector_search": "vector",
        }.get(retrieval.get("search_method"), "vector")
        display_name = self._studio_display_name(dataset)
        description = (dataset.description or dataset.name or "Dify knowledge base").strip()[:100]
        return {
            "display_name": display_name, "description": description,
            "parse_conf": {"ocr_enabled": False, "image_enabled": False, "header_footer_enabled": False, "catalog_enabled": False},
            "split_conf": split_conf, "retrieval_mode": retrieval_mode,
            # Dify 的默认检索模型会将这两个字段持久化为 null；Studio 接口
            # 需要明确的数值，按 Dify 默认值归一化。
            "top_k": int(retrieval.get("top_k") or 5),
            "score_threshold": float(retrieval.get("score_threshold") or 0),
            "rerank_enabled": bool(retrieval.get("reranking_enable", False)),
        }

    def _source_file(self, document: Document) -> tuple[UploadFile, bytes]:
        info = document.data_source_info_dict or {}
        upload_file_id = info.get("upload_file_id") or document.file_id
        upload_file = self.db.get(UploadFile, upload_file_id) if upload_file_id else None
        if upload_file is None:
            raise MigrationError(f"文档 {document.id} 未找到 Dify 上传源文件")
        return upload_file, storage.load_once(upload_file.key)

    def _segments(self, document: Document) -> list[dict[str, Any]]:
        parents = self.db.query(DocumentSegment).filter_by(document_id=document.id).order_by(DocumentSegment.position).all()
        output: list[dict[str, Any]] = []
        for parent in parents:
            item = {
                "source_segment_id": parent.id,
                "position": parent.position,
                "content": parent.content,
                "is_parent": document.doc_form == "hierarchical_model",
                "is_enabled": bool(parent.enabled),
            }
            if document.doc_form == "qa_model":
                item["answer"] = getattr(parent, "answer", None)
                if not item["answer"]:
                    raise MigrationError(f"Q&A 文档 {document.id} 的分段 {parent.id} 缺少答案")
            output.append(item)
            if document.doc_form == "hierarchical_model":
                children = self.db.query(ChildChunk).filter_by(segment_id=parent.id).order_by(ChildChunk.position).all()
                output.extend({"source_segment_id": child.id, "position": child.position, "content": child.content,
                               # Dify 1.9.1 的 child_chunks 没有 enabled 字段；子段可用性
                               # 由所属父段控制，父段已关闭时检索层自然不会使用其子段。
                               "is_parent": False, "parent_source_segment_id": parent.id, "is_enabled": True} for child in children)
        return output

    def _migrate_dataset(self, dataset: Dataset) -> int | None:
        progress = self._progress(dataset)
        if progress.status == "SUCCEEDED" and not self.force:
            return None
        documents = self.db.query(Document).filter(
            Document.dataset_id == dataset.id, Document.data_source_type == "upload_file",
            Document.indexing_status == "completed", Document.archived == False,
        ).order_by(Document.position, Document.id).all()
        if not progress.target_knowledge_repo_id:
            payload = self._knowledge_payload(dataset, documents)
            logger.info(
                "创建 Studio 知识库：name=%s split_mode=%s retrieval_mode=%s",
                payload["display_name"], payload["split_conf"].get("split_mode"), payload["retrieval_mode"],
            )
            progress.target_knowledge_repo_id = self.client.create_knowledge(payload)
            self.db.commit()
            logger.info(
                "Studio 知识库创建成功：name=%s target_knowledge_repo_id=%s",
                payload["display_name"], progress.target_knowledge_repo_id,
            )
        else:
            logger.info(
                "复用迁移目标知识库：name=%s target_knowledge_repo_id=%s",
                dataset.name, progress.target_knowledge_repo_id,
            )
        progress.total_documents = len(documents)
        # 从上一次已成功文档的下一篇继续。若源端文档已被删除，不能因找不到
        # 游标而跳过本次所有文档，改为从头执行（目标 API 的幂等键会兜底）。
        start_index = 0
        if progress.last_success_document_id:
            for index, document in enumerate(documents):
                if document.id == progress.last_success_document_id:
                    start_index = index + 1
                    break
        logger.info(
            "准备迁移文档：dataset_id=%s eligible_documents=%d start_index=%d strategy=%s",
            dataset.id, len(documents), start_index, self.strategy,
        )
        count = 0
        for document_index, document in enumerate(documents[start_index:], start=start_index + 1):
            upload_file, file_bytes = self._source_file(document)
            payload = {
                "schema_version": 2, "strategy": self.strategy,
                "source": {"system": "dify", "version": "1.9.1", "instance": self.settings.instance_id,
                           "dataset_id": dataset.id, "document_id": document.id},
                "idempotency_key": hashlib.sha256(f"{self.settings.instance_id}:{dataset.id}:{document.id}:{self.strategy}".encode()).hexdigest(),
                "content_sha256": hashlib.sha256(file_bytes).hexdigest(),
            }
            if self.strategy == "segments":
                payload["segments"] = self._segments(document)
            segment_count = len(payload.get("segments", []))
            logger.info(
                "[文档 %d/%d] 开始导入：dataset_id=%s document_id=%s file=%s bytes=%d segments=%d",
                document_index, len(documents), dataset.id, document.id, upload_file.name, len(file_bytes), segment_count,
            )
            started_at = time.perf_counter()
            response = self.client.import_document(
                progress.target_knowledge_repo_id, payload, upload_file.name, file_bytes, upload_file.mime_type or "",
            )
            progress.last_success_document_id = document.id
            progress.succeeded_documents += 1
            self.db.commit()
            count += 1
            logger.info(
                "[文档 %d/%d] 导入成功：document_id=%s target_document_id=%s elapsed_seconds=%.2f progress=%d/%d",
                document_index, len(documents), document.id,
                response.get("document_id", response.get("id", "unknown")), time.perf_counter() - started_at,
                start_index + count, len(documents),
            )
        progress.status = "SUCCEEDED"
        progress.completed_at = datetime.utcnow()
        self.db.commit()
        logger.info(
            "知识库文档迁移完成：dataset_id=%s target_knowledge_repo_id=%s total_documents=%d imported_documents=%d",
            dataset.id, progress.target_knowledge_repo_id, len(documents), count,
        )
        return count

    def _mark_dataset_failed(self, dataset: Dataset, error: str) -> None:
        progress = self.db.query(DatasetMigrationProgress).filter_by(
            tenant_id=self.tenant_id, dataset_id=dataset.id, strategy=self.strategy,
            project_id=self.settings.project_id, workspace_id=self.settings.workspace_id,
        ).one_or_none()
        if progress:
            progress.status = "FAILED"
            progress.failed_documents += 1
            progress.error_message = error[:4000]
            self.db.commit()
            logger.info("已记录迁移失败进度：dataset_id=%s failed_documents=%d", dataset.id, progress.failed_documents)
