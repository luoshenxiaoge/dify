"""Dify 1.9.1 到 Studio 知识库的迁移器。

迁移器只通过 Studio Manager 对外 API 写入目标系统。source_file 策略上传原文件，
segments 策略上传文件及 Dify 已有分段，源文件缺失时用分段重建 TXT；两条链路互不混用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from opendal.exceptions import NotFound
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from core.model_manager import ModelInstance, ModelManager
from core.model_runtime.entities.model_entities import ModelType
from extensions.ext_storage import storage
from models.dataset import ChildChunk, Dataset, Document, DocumentSegment
from models.dataset_migration import DatasetMigrationProgress
from models.model import UploadFile

logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """目标 API 或源数据不满足迁移约束。"""


@dataclass(frozen=True)
class MigrationSourceFile:
    name: str
    mime_type: str
    reconstructed_from_segments: bool = False
    original_name: str | None = None


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
    ) -> MigrationSettings:
        """从本次 CLI 显式参数构造目标 Studio 迁移范围。"""
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

    def ensure_model(self, payload: dict[str, object]) -> dict[str, str]:
        response = self.session.post(
            self._url(f"/knowledges/dify-models:ensure?workspace_id={self.settings.workspace_id}"),
            json=payload,
            timeout=60,
        )
        # 避免代理或参数错误响应回显凭据；仅保留错误码。
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        data = body.get("data", body)
        if (
            not response.ok
            or body.get("error_code")
            or not isinstance(data, dict)
            or not data.get("id")
            or not data.get("name")
        ):
            code = str(body.get("error_code", ""))
            safe_code = code if re.fullmatch(r"openjiuwen\.\d+", code) else "unknown"
            raise MigrationError(f"Studio 模型注册失败：HTTP {response.status_code}，错误码 {safe_code}")
        return {"id": str(data["id"]), "name": str(data["name"])}

    def create_knowledge(self, payload: dict[str, Any]) -> str:
        response = self.session.post(
            self._url(f"/knowledges?workspace_id={self.settings.workspace_id}"),
            json=payload,
            timeout=60,
        )
        data = self._data(response)
        for key in ("knowledge_repo_id", "knowledgeRepoId", "id"):
            if data.get(key):
                return str(data[key])
        raise MigrationError(f"创建知识库响应缺少 knowledge_repo_id：{data}")

    def import_document(
        self, knowledge_repo_id: str, payload: Mapping[str, object], file_name: str, file_bytes: bytes, mime_type: str
    ) -> dict[str, Any]:
        suffix = f"/knowledges/{knowledge_repo_id}/migrations/dify/documents:"
        strategy = payload["strategy"]
        endpoint = "upload" if strategy == "source_file" else "import"
        upload_name = str(payload.get("source_file_name") or file_name)
        files: dict[str, Any] = {"file": (upload_name, file_bytes, mime_type or "application/octet-stream")}
        if strategy == "segments":
            files["payload"] = (None, json.dumps(payload, ensure_ascii=False), "application/json")
        else:
            files["document_name"] = (None, str(payload.get("document_name") or file_name))
        response = self.session.post(
            self._url(f"{suffix}{endpoint}?workspace_id={self.settings.workspace_id}"),
            files=files,
            timeout=300,
        )
        return self._data(response)


class DifyKnowledgeMigrator:
    """按 tenant 过滤源数据并以文档粒度推进进度。"""

    def __init__(
        self,
        session: Session,
        client: StudioManagerClient,
        settings: MigrationSettings,
        tenant_id: str,
        strategy: str,
        force: bool = False,
    ):
        if strategy not in {"source_file", "segments"}:
            raise MigrationError("strategy 只能是 source_file 或 segments")
        self.db = session
        self.client = client
        self.settings = settings
        self.tenant_id = tenant_id
        self.strategy = strategy
        self.force = force
        self._has_source_extract_type: bool | None = None
        self._source_model_refs: dict[tuple[ModelType, str, str], dict[str, str]] = {}
        self.fingerprint = hashlib.sha256(
            f"{strategy}|{settings.project_id}|{settings.workspace_id}|{settings.instance_id}".encode()
        ).hexdigest()

    def migrate(
        self, dataset_ids: Iterable[str] | None = None, excluded_dataset_ids: Iterable[str] | None = None
    ) -> dict[str, int]:
        requested = set(dataset_ids or [])
        excluded = set(excluded_dataset_ids or [])
        query = (
            self.db.query(Dataset)
            .filter(
                Dataset.tenant_id == self.tenant_id,
                Dataset.indexing_technique == "high_quality",
            )
            .order_by(Dataset.created_at, Dataset.id)
        )
        datasets = [item for item in query if (not requested or item.id in requested) and item.id not in excluded]
        result = {"datasets": 0, "succeeded": 0, "skipped": 0, "failed": 0, "documents": 0}
        logger.info(
            "Dify 知识库迁移开始：strategy=%s tenant_id=%s candidates=%d specified_datasets=%d excluded_datasets=%d",
            self.strategy,
            self.tenant_id,
            len(datasets),
            len(requested),
            len(excluded),
        )
        for dataset_index, dataset in enumerate(datasets, start=1):
            result["datasets"] += 1
            try:
                logger.info(
                    "[知识库 %d/%d] 开始：name=%s dataset_id=%s strategy=%s",
                    dataset_index,
                    len(datasets),
                    dataset.name,
                    dataset.id,
                    self.strategy,
                )
                imported_documents = self._migrate_dataset(dataset)
                if imported_documents is None:
                    result["skipped"] += 1
                    continue
                result["documents"] += imported_documents
                result["succeeded"] += 1
                logger.info(
                    "[知识库 %d/%d] 完成：name=%s imported_documents=%d",
                    dataset_index,
                    len(datasets),
                    dataset.name,
                    imported_documents,
                )
            except Exception as exc:
                result["failed"] += 1
                logger.exception(
                    "[知识库 %d/%d] 失败：name=%s dataset_id=%s",
                    dataset_index,
                    len(datasets),
                    dataset.name,
                    dataset.id,
                )
                self._mark_dataset_failed(dataset, str(exc))
        logger.info("Dify 知识库迁移结束：%s", result)
        return result

    def _progress(self, dataset: Dataset) -> DatasetMigrationProgress:
        progress = (
            self.db.query(DatasetMigrationProgress)
            .filter_by(
                tenant_id=self.tenant_id,
                dataset_id=dataset.id,
                strategy=self.strategy,
                project_id=self.settings.project_id,
                workspace_id=self.settings.workspace_id,
            )
            .one_or_none()
        )
        if progress is None:
            progress = DatasetMigrationProgress(
                tenant_id=self.tenant_id,
                dataset_id=dataset.id,
                strategy=self.strategy,
                project_id=self.settings.project_id,
                workspace_id=self.settings.workspace_id,
                parameter_fingerprint=self.fingerprint,
                status="PENDING",
            )
            self.db.add(progress)
            self.db.flush()
            logger.info("创建迁移进度：dataset_id=%s strategy=%s", dataset.id, self.strategy)
        if progress.status == "SUCCEEDED" and not self.force:
            logger.info(
                "跳过已成功知识库：name=%s dataset_id=%s target_knowledge_repo_id=%s",
                dataset.name,
                dataset.id,
                progress.target_knowledge_repo_id,
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
            dataset.name,
            dataset.id,
            previous_status,
            progress.last_success_document_id,
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

    def _source_extract_type(self, dataset: Dataset) -> str:
        """兼容生产新增列，读取时不要求本地 ORM 或旧版数据库增加字段。"""
        if hasattr(dataset, "extract_type"):
            return str(getattr(dataset, "extract_type", None) or "default")
        if self._has_source_extract_type is None:
            columns = inspect(self.db.connection()).get_columns(Dataset.__tablename__)
            self._has_source_extract_type = any(column["name"] == "extract_type" for column in columns)
        if not self._has_source_extract_type:
            return "default"
        value = self.db.execute(
            text("SELECT extract_type FROM datasets WHERE id = :dataset_id"), {"dataset_id": dataset.id}
        ).scalar_one_or_none()
        return str(value or "default")

    def _knowledge_payload(
        self, dataset: Dataset, documents: list[Document], *, extract_type: str | None = None
    ) -> dict[str, Any]:
        process_rule = dataset.latest_process_rule
        mode = process_rule.mode if process_rule else "automatic"
        rules = process_rule.rules_dict if process_rule and process_rule.rules_dict else {}
        remove_spaces, remove_urls = self._preprocessing(rules)
        segmentation = rules.get("segmentation") or {"separator": "\n\n", "max_tokens": 500, "chunk_overlap": 50}
        separator = segmentation.get("separator", segmentation.get("delimiter", "\n\n"))
        qa_documents = [document for document in documents if document.doc_form == "qa_model"]
        languages: set[str] = set()
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
                "split_mode": "PARENT_CHILD",
                "dify_compatible": True,
                "parent_mode": rules.get("parent_mode", "paragraph"),
                "parent_max_tokens": int(segmentation.get("max_tokens", 1024)),
                "parent_chunk_overlap": int(segmentation.get("chunk_overlap", 0)),
                "parent_delimiter": separator,
                "child_max_tokens": int(child.get("max_tokens", 512)),
                "child_chunk_overlap": int(child.get("chunk_overlap", 0)),
                "child_delimiter": child.get("separator", child.get("delimiter", "\n")),
                "remove_extra_spaces": remove_spaces,
                "remove_urls_emails": remove_urls,
            }
        else:
            split_conf = {
                "split_mode": "GENERAL",
                "dify_compatible": True,
                "separator": separator,
                "max_tokens": int(segmentation.get("max_tokens", 500)),
                "chunk_overlap": int(segmentation.get("chunk_overlap", 50)),
                "remove_extra_spaces": remove_spaces,
                "remove_urls_emails": remove_urls,
            }
        if qa_documents and self.strategy == "source_file":
            split_conf = {
                "split_mode": "GENERAL",
                "dify_compatible": True,
                "separator": separator,
                "max_tokens": int(segmentation.get("max_tokens", 500)),
                "chunk_overlap": int(segmentation.get("chunk_overlap", 50)),
                "remove_extra_spaces": remove_spaces,
                "remove_urls_emails": remove_urls,
                "qa_enabled": True,
                "qa_language": next(iter(languages)),
            }
        retrieval = dataset.retrieval_model or {}
        # Dify 的检索枚举与 Studio 的对外枚举并不完全同名，不能直接移除
        # "_search" 后缀，否则 semantic_search 会得到 Studio 不支持的 semantic。
        retrieval_mode = {
            "semantic_search": "vector",
            "full_text_search": "keyword",
            "hybrid_search": "hybrid",
            "vector_search": "vector",
        }.get(str(retrieval.get("search_method") or ""), "vector")
        display_name = self._studio_display_name(dataset)
        description = (dataset.description or dataset.name or "Dify knowledge base").strip()[:100]
        # 生产 datasets.extract_type：default 为普通解析，其余解析类型统一映射到 Studio OCR。
        # 兼容未增加该字段的 Dify 版本，以及尚未填充解析类型的旧数据。
        extract_type = extract_type or getattr(dataset, "extract_type", None) or "default"
        return {
            "display_name": display_name,
            "description": description,
            "parse_conf": {
                "ocr_enabled": extract_type != "default",
                "image_enabled": False,
                "header_footer_enabled": False,
                "catalog_enabled": False,
            },
            "split_conf": split_conf,
            "retrieval_mode": retrieval_mode,
            # Dify 的默认检索模型会将这两个字段持久化为 null；Studio 接口
            # 需要明确的数值，按 Dify 默认值归一化。
            # 与生产 Dataset.retrieval_model_dict 的普通知识库默认值一致。
            "top_k": int(retrieval.get("top_k") or 2),
            "score_threshold": float(retrieval.get("score_threshold") or 0),
            "rerank_enabled": bool(retrieval.get("reranking_enable", False)),
        }

    @staticmethod
    def _model_endpoint(instance: ModelInstance, model_type: ModelType) -> tuple[str, str]:
        credentials = instance.credentials
        provider = instance.provider.rsplit("/", 1)[-1]
        api_key = str(
            credentials.get("api_key")
            or credentials.get("openai_api_key")
            or credentials.get("dashscope_api_key")
            or ""
        )
        base = (
            str(
                credentials.get("endpoint_url")
                or credentials.get("openai_api_base")
                or credentials.get("base_url")
                or credentials.get("api_base")
                or ""
            )
            .strip()
            .rstrip("/")
        )
        suffix = {
            ModelType.TEXT_EMBEDDING: "/embeddings",
            ModelType.RERANK: "/rerank",
            ModelType.LLM: "/chat/completions",
        }[model_type]
        rerank_endpoint = str(credentials.get("rerank_endpoint_url") or "").strip().rstrip("/")
        if model_type == ModelType.RERANK and rerank_endpoint:
            base, suffix = rerank_endpoint, ""
        elif not base and provider == "tongyi":
            host = str(credentials.get("api_host") or "").strip().rstrip("/").removesuffix("/api/v1")
            if not host:
                international = str(credentials.get("use_international_endpoint", "")).lower() == "true"
                host = "https://dashscope-intl.aliyuncs.com" if international else "https://dashscope.aliyuncs.com"
            if model_type == ModelType.RERANK:
                base, suffix = host + "/compatible-api/v1", "/reranks"
            else:
                base = host + "/compatible-mode/v1"
        elif not base and provider == "openai" and model_type != ModelType.RERANK:
            base = "https://api.openai.com/v1"
        endpoint = (
            base
            if not suffix or base.endswith((suffix, "/reranks" if model_type == ModelType.RERANK else suffix))
            else base + suffix
        )
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise MigrationError(f"Dify {model_type.value} 模型没有可迁移的 HTTP 调用地址，请检查供应商配置")
        return endpoint, api_key

    def _attach_source_models(
        self, dataset: Dataset, payload: dict[str, Any], *, model_manager: ModelManager | None = None
    ) -> None:
        retrieval = dataset.retrieval_model or {}
        rerank = retrieval.get("reranking_model") or {}
        selections = (
            (
                ModelType.TEXT_EMBEDDING,
                getattr(dataset, "embedding_model_provider", None),
                getattr(dataset, "embedding_model", None),
                "embedding_model",
                "embedding",
            ),
            (
                ModelType.RERANK,
                rerank.get("reranking_provider_name"),
                rerank.get("reranking_model_name"),
                "rerank_model",
                "rerank",
            ),
        )
        manager = model_manager or ModelManager()
        for model_type, provider, name, field, ref_type in selections:
            if not provider and not name:
                continue
            if not provider or not name:
                raise MigrationError(f"Dify {model_type.value} 模型的供应商或名称缺失")
            ref = self._ensure_source_model(manager, model_type, str(provider), str(name))
            payload[field] = {"type": ref_type, **ref}
        if payload["split_conf"].get("qa_enabled"):
            ref = self._ensure_source_model(manager, ModelType.LLM, "", "")
            payload["split_conf"]["qa_model"] = ref["name"]

    def _ensure_source_model(
        self, manager: ModelManager, model_type: ModelType, provider: str, name: str
    ) -> dict[str, str]:
        identity = (model_type, provider, name)
        if identity in self._source_model_refs:
            return self._source_model_refs[identity]
        try:
            instance = (
                manager.get_model_instance(
                    tenant_id=self.tenant_id, provider=provider, model_type=model_type, model=name
                )
                if provider
                else manager.get_default_model_instance(self.tenant_id, model_type)
            )
            endpoint, api_key = self._model_endpoint(instance, model_type)
            model_name = str(instance.credentials.get("endpoint_model_name") or instance.model)
            config: dict[str, object] = {
                "provider": instance.provider,
                "model_name": model_name,
                "model_type": model_type.value,
                "api_url": endpoint,
                "api_key": api_key,
            }
            if model_type == ModelType.TEXT_EMBEDDING:
                result = instance.invoke_text_embedding(["Studio model migration"], user=self.tenant_id)
                if not result.embeddings or not result.embeddings[0]:
                    raise MigrationError("Dify Embedding 模型未返回可用向量，无法确定维度")
                config["dimension"] = len(result.embeddings[0])
        except MigrationError:
            raise
        except Exception as exc:
            label = name if provider else "默认 LLM"
            # 供应商异常可能包含凭据或完整 HTTP 响应，避免向迁移日志传播。
            raise MigrationError(f"读取 Dify {model_type.value} 模型 {label} 失败（{type(exc).__name__}）") from None
        ref = self.client.ensure_model(config)
        self._source_model_refs[identity] = ref
        return ref

    def _source_file(
        self, document: Document, segments: Sequence[Mapping[str, object]] | None = None
    ) -> tuple[MigrationSourceFile, bytes]:
        info = document.data_source_info_dict or {}
        upload_file_id = info.get("upload_file_id") or document.file_id
        upload_file = self.db.get(UploadFile, upload_file_id) if upload_file_id else None
        if upload_file is not None:
            try:
                if self.strategy == "segments" and not storage.exists(upload_file.key):
                    raise FileNotFoundError("File not found")
                content = storage.load_once(upload_file.key)
            except (FileNotFoundError, NotFound):
                if self.strategy != "segments":
                    raise
            else:
                return MigrationSourceFile(
                    name=document.name or upload_file.name,
                    mime_type=upload_file.mime_type or "application/octet-stream",
                    original_name=upload_file.name,
                ), content
        elif self.strategy != "segments":
            raise MigrationError(f"文档 {document.id} 未找到 Dify 上传源文件")

        source_segments = segments if segments is not None else self._segments(document)
        return self._source_file_from_segments(document, source_segments)

    def _source_file_from_segments(
        self, document: Document, segments: Sequence[Mapping[str, object]]
    ) -> tuple[MigrationSourceFile, bytes]:
        if not any(str(segment.get("content") or "").strip() for segment in segments):
            raise MigrationError(f"文档 {document.id} 源文件缺失，且没有可用分段，无法生成替代 TXT")

        blocks = [f"文档：{document.name}\nDify 文档ID：{document.id}\n说明：源文件缺失，本文档由 Dify 已有分段重建。"]
        for index, segment in enumerate(segments, start=1):
            parent_id = segment.get("parent_source_segment_id")
            kind = "子分段" if parent_id else ("父分段" if segment.get("is_parent") else "分段")
            lines = [f"【{kind} {index}】", f"分段ID：{segment.get('source_segment_id') or ''}"]
            if parent_id:
                lines.append(f"父分段ID：{parent_id}")
            content = str(segment.get("content") or "")
            if segment.get("answer") is not None:
                lines.extend((f"问题：{content}", f"答案：{segment['answer']}"))
            else:
                lines.append(content)
            blocks.append("\n".join(lines))

        original_name = Path(str(document.name or document.id).replace("\\", "/")).name
        filename = f"{Path(original_name).stem[:240] or document.id}-分段重建.txt"
        content_bytes = ("\n\n".join(blocks) + "\n").encode("utf-8")
        logger.warning(
            "Dify 源文件缺失，使用已有分段重建 TXT：document_id=%s file=%s segments=%d bytes=%d",
            document.id,
            filename,
            len(segments),
            len(content_bytes),
        )
        return MigrationSourceFile(filename, "text/plain", reconstructed_from_segments=True), content_bytes

    def _segments(self, document: Document) -> list[dict[str, Any]]:
        parents = (
            self.db.query(DocumentSegment).filter_by(document_id=document.id).order_by(DocumentSegment.position).all()
        )
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
                output.extend(
                    {
                        "source_segment_id": child.id,
                        "position": child.position,
                        "content": child.content,
                        # Dify 1.9.1 的 child_chunks 没有 enabled 字段；子段可用性
                        # 由所属父段控制，父段已关闭时检索层自然不会使用其子段。
                        "is_parent": False,
                        "parent_source_segment_id": parent.id,
                        "is_enabled": True,
                    }
                    for child in children
                )
        return output

    def _migrate_dataset(self, dataset: Dataset) -> int | None:
        progress = self._progress(dataset)
        if progress.status == "SUCCEEDED" and not self.force:
            return None
        documents = (
            self.db.query(Document)
            .filter(
                Document.dataset_id == dataset.id,
                Document.data_source_type == "upload_file",
                Document.indexing_status == "completed",
                Document.archived == False,
                Document.enabled == True,
            )
            .order_by(Document.position, Document.id)
            .all()
        )
        if not progress.target_knowledge_repo_id:
            payload = self._knowledge_payload(dataset, documents, extract_type=self._source_extract_type(dataset))
            self._attach_source_models(dataset, payload)
            logger.info(
                "创建 Studio 知识库：name=%s split_mode=%s retrieval_mode=%s",
                payload["display_name"],
                payload["split_conf"].get("split_mode"),
                payload["retrieval_mode"],
            )
            progress.target_knowledge_repo_id = self.client.create_knowledge(payload)
            self.db.commit()
            logger.info(
                "Studio 知识库创建成功：name=%s target_knowledge_repo_id=%s",
                payload["display_name"],
                progress.target_knowledge_repo_id,
            )
        else:
            logger.info(
                "复用迁移目标知识库：name=%s target_knowledge_repo_id=%s",
                dataset.name,
                progress.target_knowledge_repo_id,
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
            dataset.id,
            len(documents),
            start_index,
            self.strategy,
        )
        count = 0
        for document_index, document in enumerate(documents[start_index:], start=start_index + 1):
            segments = self._segments(document) if self.strategy == "segments" else None
            upload_file, file_bytes = self._source_file(document, segments)
            source: dict[str, str | bool] = {
                "system": "dify",
                "version": "1.9.1",
                "instance": self.settings.instance_id,
                "dataset_id": dataset.id,
                "document_id": document.id,
            }
            if upload_file.reconstructed_from_segments:
                source["source_file_reconstructed"] = True
                source["source_file_original_name"] = document.name
            payload: dict[str, object] = {
                "document_name": document.name,
                "source_file_name": upload_file.original_name or upload_file.name,
                "schema_version": 2,
                "strategy": self.strategy,
                "source": source,
                "idempotency_key": hashlib.sha256(
                    f"{self.settings.instance_id}:{dataset.id}:{document.id}:{self.strategy}".encode()
                ).hexdigest(),
                "content_sha256": hashlib.sha256(file_bytes).hexdigest(),
            }
            if segments is not None:
                payload["segments"] = segments
            segment_count = len(segments or [])
            logger.info(
                "[文档 %d/%d] 开始导入：dataset_id=%s document_id=%s file=%s bytes=%d segments=%d",
                document_index,
                len(documents),
                dataset.id,
                document.id,
                upload_file.name,
                len(file_bytes),
                segment_count,
            )
            started_at = time.perf_counter()
            response = self.client.import_document(
                progress.target_knowledge_repo_id,
                payload,
                upload_file.name,
                file_bytes,
                upload_file.mime_type or "",
            )
            progress.last_success_document_id = document.id
            progress.succeeded_documents += 1
            self.db.commit()
            count += 1
            logger.info(
                "[文档 %d/%d] 导入成功：document_id=%s target_document_id=%s elapsed_seconds=%.2f progress=%d/%d",
                document_index,
                len(documents),
                document.id,
                response.get("document_id", response.get("id", "unknown")),
                time.perf_counter() - started_at,
                start_index + count,
                len(documents),
            )
        progress.status = "SUCCEEDED"
        progress.completed_at = datetime.now(UTC).replace(tzinfo=None)
        self.db.commit()
        logger.info(
            "知识库文档迁移完成：dataset_id=%s target_knowledge_repo_id=%s total_documents=%d imported_documents=%d",
            dataset.id,
            progress.target_knowledge_repo_id,
            len(documents),
            count,
        )
        return count

    def _mark_dataset_failed(self, dataset: Dataset, error: str) -> None:
        progress = (
            self.db.query(DatasetMigrationProgress)
            .filter_by(
                tenant_id=self.tenant_id,
                dataset_id=dataset.id,
                strategy=self.strategy,
                project_id=self.settings.project_id,
                workspace_id=self.settings.workspace_id,
            )
            .one_or_none()
        )
        if progress:
            progress.status = "FAILED"
            progress.failed_documents += 1
            progress.error_message = error[:4000]
            self.db.commit()
            logger.info("已记录迁移失败进度：dataset_id=%s failed_documents=%d", dataset.id, progress.failed_documents)
