"""Dify 到 Studio 知识库迁移的可恢复进度记录。"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .types import StringUUID


class DatasetMigrationProgress(Base):
    """以源知识库、策略和目标空间为粒度记录迁移断点。"""

    # Dify 现有数据库可能已包含同名的业务表；迁移器使用独立表，避免改写既有结构。
    __tablename__ = "dify_kb_migration_progress"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="dify_kb_migration_progress_pkey"),
        sa.UniqueConstraint(
            "tenant_id", "dataset_id", "strategy", "project_id", "workspace_id",
            name="dify_kb_migration_progress_scope_uk",
        ),
        sa.Index("dify_kb_migration_progress_status_idx", "tenant_id", "strategy", "status"),
    )

    id: Mapped[str] = mapped_column(StringUUID, server_default=sa.text("uuid_generate_v4()"))
    tenant_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    dataset_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_knowledge_repo_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=sa.text("'PENDING'"))
    last_success_document_id: Mapped[str | None] = mapped_column(StringUUID, nullable=True)
    total_documents: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    succeeded_documents: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    failed_documents: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    parameter_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.current_timestamp(), onupdate=func.current_timestamp())
