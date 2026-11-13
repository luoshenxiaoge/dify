from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models.account import Account, TenantAccountRole
from models.dataset import Dataset
from services import dataset_service
from services import dify_kb_migration_runs as runs
from services.dify_kb_migration import MigrationError
from services.dify_kb_migration_runs import MigrationSourceScope, console_migration_datasets
from tasks import dify_kb_migration_task


@pytest.fixture
def source_catalog():
    tenant, other_tenant, actor, other_actor, tag = (str(uuid4()) for _ in range(5))
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE datasets (id TEXT, tenant_id TEXT, name TEXT, permission TEXT, "
            "created_by TEXT, indexing_technique TEXT, created_at TEXT)"
        )
        conn.exec_driver_sql("CREATE TABLE dataset_permissions (dataset_id TEXT, tenant_id TEXT, account_id TEXT)")
        conn.exec_driver_sql("CREATE TABLE tags (id TEXT, tenant_id TEXT, type TEXT)")
        conn.exec_driver_sql("CREATE TABLE tag_bindings (target_id TEXT, tenant_id TEXT, tag_id TEXT)")
        visible = [str(uuid4()) for _ in range(103)]
        hidden = [str(uuid4()) for _ in range(310)]
        private, economy, foreign = (str(uuid4()) for _ in range(3))
        rows = [
            *(
                (item, tenant, "Manual visible", "all_team_members", other_actor, "high_quality", "2026-11-12")
                for item in visible
            ),
            *(
                (item, tenant, "Manual untagged", "only_me", other_actor, "high_quality", "2026-11-12")
                for item in hidden
            ),
            (private, tenant, "Manual private", "only_me", other_actor, "high_quality", "2026-11-12"),
            (economy, tenant, "Manual economy", "all_team_members", other_actor, "economy", "2026-11-12"),
            (foreign, other_tenant, "Manual foreign", "all_team_members", other_actor, "high_quality", "2026-11-12"),
        ]
        conn.exec_driver_sql("INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.exec_driver_sql("INSERT INTO tags VALUES (?, ?, ?)", (tag, tenant, "knowledge"))
        conn.exec_driver_sql(
            "INSERT INTO tag_bindings VALUES (?, ?, ?)",
            [(item, tenant, tag) for item in [*visible, private, economy, foreign]],
        )
    with Session(engine) as session:
        yield session, tenant, actor, tag, set(visible), private, economy
    engine.dispose()


def test_owner_scope_matches_filtered_list_across_pages(source_catalog, monkeypatch):
    session, tenant, actor, tag, visible, private, economy = source_catalog
    scope = MigrationSourceScope(include_all=True, keyword="Manual", tag_ids=[UUID(tag)])
    candidates = console_migration_datasets(tenant, actor, TenantAccountRole.OWNER, scope).with_only_columns(Dataset.id)
    original_session = Mock()
    original_session.query.return_value.filter_by.return_value.all.return_value = []
    original_session.query.return_value.where.return_value.group_by.return_value.all.return_value = []
    original_session.query.return_value.join.return_value.where.return_value.all.return_value = []

    def paginate(**options):
        ids = session.scalars(options["select"].with_only_columns(Dataset.id)).all()
        return SimpleNamespace(items=[SimpleNamespace(id=item) for item in ids], total=len(ids))

    monkeypatch.setattr(dataset_service, "db", SimpleNamespace(session=original_session, paginate=paginate))
    monkeypatch.setattr(
        dataset_service.TagService, "get_target_ids_by_tag_ids", Mock(return_value=[*visible, private, economy])
    )
    items, total = dataset_service.DatasetService.get_datasets(
        1, 100, tenant, Mock(id=actor, current_role=TenantAccountRole.OWNER), "Manual", [tag], True
    )
    assert {item.id for item in items} == visible | {private, economy}
    assert total == 105
    pages = [session.scalars(candidates.offset(start).limit(20)).all() for start in range(0, 120, 20)]
    ids = [item for page in pages for item in page]
    assert len(ids) == 104
    assert set(ids) == visible | {private}


def test_owner_without_all_cannot_select_someone_elses_private_dataset(source_catalog):
    session, tenant, actor, tag, visible, private, _ = source_catalog
    scope = MigrationSourceScope(keyword="Manual", tag_ids=[UUID(tag)])
    candidates = console_migration_datasets(tenant, actor, TenantAccountRole.OWNER, scope).with_only_columns(Dataset.id)
    assert set(session.scalars(candidates)) == visible
    assert session.scalar(candidates.where(Dataset.id == private)) is None


def test_new_runs_and_retries_apply_current_scope_even_to_explicit_ids(source_catalog, monkeypatch):
    session, tenant, actor_id, tag, visible, private, economy = source_catalog
    actor = Mock(spec=Account, id=actor_id, current_tenant_id=tenant, current_role=TenantAccountRole.OWNER)
    db_session = Mock()
    db_session.scalar.return_value = None
    db_session.execute.side_effect = lambda query: session.execute(query) if "FROM datasets" in str(query) else Mock()
    monkeypatch.setattr(runs, "db", SimpleNamespace(session=db_session))
    monkeypatch.setattr(runs, "expire_idle_runs", Mock())
    monkeypatch.setattr(runs, "redis_client", Mock())
    monkeypatch.setattr(runs, "encrypt_token", Mock(return_value="encrypted"))
    monkeypatch.setattr(dify_kb_migration_task.run_dify_kb_migration, "delay", Mock())
    chosen = next(iter(visible))
    options = runs.MigrationRunOptions(
        studio_manager_url="https://studio.example.com",
        project_id="0",
        workspace_id="target",
        tenant_id=UUID(tenant),
        strategy="source_file",
        source_scope=MigrationSourceScope(include_all=True, keyword="Manual", tag_ids=[UUID(tag)]),
        dataset_ids=[UUID(item) for item in (chosen, private, economy)],
        excluded_dataset_ids=[UUID(chosen)],
    )

    run = runs.start_run(options, "api-key", actor)
    assert set(run.datasets) == {private}
    assert run.options["source_account_id"] == actor_id

    run.status = "FAILED"
    actor.current_role = TenantAccountRole.ADMIN
    options = runs.retry_options(run)
    assert options.source_scope.include_all
    with pytest.raises(MigrationError, match="no eligible"):
        runs.start_run(options, "api-key", actor, retry_of=run.id)


def test_source_endpoint_paginates_search_results_with_separate_scope_total(source_catalog, monkeypatch):
    from inspect import unwrap

    from flask import Flask

    from controllers.console.datasets import migration as controller

    session, tenant, actor_id, tag, _, _, _ = source_catalog
    monkeypatch.setattr(controller, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(controller, "_tenant_id", lambda: tenant)
    monkeypatch.setattr(controller, "current_user", Mock(id=actor_id, current_role=TenantAccountRole.OWNER))
    app = Flask(__name__)
    get = unwrap(controller.DatasetMigrationSourcesApi.get)

    with app.test_request_context(query_string={"include_all": "true", "tag_ids": tag, "page": 2}):
        response = get(controller.DatasetMigrationSourcesApi())
    assert response["total"] == response["scope_total"] == 104
    assert len(response["data"]) == 20
    assert response["has_more"]

    with app.test_request_context(query_string={"include_all": "true", "tag_ids": tag, "keyword": "private"}):
        response = get(controller.DatasetMigrationSourcesApi())
    assert response["total"] == 1
    assert response["scope_total"] == 104
    assert len(response["data"]) == 1
    assert not response["has_more"]
