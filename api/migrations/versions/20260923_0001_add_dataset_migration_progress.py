"""add dataset migration progress

Revision ID: 20260923dmp1
Revises: 68519ad5cd18
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa
import models


revision = "20260923dmp1"
down_revision = "68519ad5cd18"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dify_kb_migration_progress",
        sa.Column("id", models.types.StringUUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", models.types.StringUUID(), nullable=False),
        sa.Column("dataset_id", models.types.StringUUID(), nullable=False),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("target_knowledge_repo_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("last_success_document_id", models.types.StringUUID(), nullable=True),
        sa.Column("total_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("succeeded_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_documents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("parameter_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="dify_kb_migration_progress_pkey"),
        sa.UniqueConstraint("tenant_id", "dataset_id", "strategy", "project_id", "workspace_id", name="dify_kb_migration_progress_scope_uk"),
    )
    op.create_index("dify_kb_migration_progress_status_idx", "dify_kb_migration_progress", ["tenant_id", "strategy", "status"])


def downgrade():
    op.drop_index("dify_kb_migration_progress_status_idx", table_name="dify_kb_migration_progress")
    op.drop_table("dify_kb_migration_progress")
