"""Add knowledge migration runs for the Dify console."""

import sqlalchemy as sa
from alembic import op

from models.types import StringUUID

revision = "20261109dmr1"
down_revision = "20260923dmp1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dify_kb_migration_runs",
        sa.Column("id", StringUUID(), nullable=False),
        sa.Column("tenant_id", StringUUID(), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("datasets", sa.JSON(), nullable=False),
        sa.Column("logs", sa.JSON(), nullable=False),
        sa.Column("retry_of", StringUUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="dify_kb_migration_runs_pkey"),
    )
    op.create_index("dify_kb_migration_runs_tenant_created_idx", "dify_kb_migration_runs", ["tenant_id", "created_at"])


def downgrade():
    op.drop_table("dify_kb_migration_runs")
