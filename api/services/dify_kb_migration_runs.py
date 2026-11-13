"""Run the CLI migrator in a worker and retain console-safe progress."""

import logging
from datetime import UTC, datetime, timedelta
from ipaddress import IPv6Address, ip_address
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, BaseModel, Field, StringConstraints
from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.orm import Session

from core.helper.encrypter import decrypt_token, encrypt_token
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from models.dataset import Dataset, DatasetPermission, DatasetPermissionEnum
from models.dataset_migration import DatasetMigrationRun
from models.model import Tag, TagBinding
from services.dify_kb_migration import (
    DifyKnowledgeMigrator,
    MigrationError,
    MigrationSettings,
    StudioManagerClient,
)

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ("QUEUED", "RUNNING")
DONE_DATASET_STATUSES = ("SUCCEEDED", "SKIPPED")
IDLE_TIMEOUT = timedelta(minutes=30)
LOG_LIMIT = 250
TargetId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
ApiKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
ManagerUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


class MigrationSourceScope(BaseModel):
    include_all: bool = False
    keyword: str = Field(default="", max_length=255)
    tag_ids: list[UUID] = Field(default_factory=list)


def console_migration_datasets(
    tenant_id: str, account_id: str, role: TenantAccountRole | str | None, scope: MigrationSourceScope
) -> Select[tuple[Dataset]]:
    query = (
        select(Dataset)
        .where(Dataset.tenant_id == tenant_id, Dataset.indexing_technique == "high_quality")
        .order_by(Dataset.created_at.desc(), Dataset.id)
    )
    # Mirror the knowledge list's visibility only within the migration feature.
    if not (scope.include_all and role == TenantAccountRole.OWNER):
        permitted_ids = select(DatasetPermission.dataset_id).where(
            DatasetPermission.account_id == account_id, DatasetPermission.tenant_id == tenant_id
        )
        if role == TenantAccountRole.DATASET_OPERATOR:
            query = query.where(Dataset.id.in_(permitted_ids))
        else:
            query = query.where(
                or_(
                    Dataset.permission == DatasetPermissionEnum.ALL_TEAM,
                    and_(Dataset.permission == DatasetPermissionEnum.ONLY_ME, Dataset.created_by == account_id),
                    and_(Dataset.permission == DatasetPermissionEnum.PARTIAL_TEAM, Dataset.id.in_(permitted_ids)),
                )
            )
    if scope.keyword:
        query = query.where(Dataset.name.ilike(f"%{scope.keyword}%"))
    if scope.tag_ids:
        targets = (
            select(TagBinding.target_id)
            .join(Tag, Tag.id == TagBinding.tag_id)
            .where(
                Tag.id.in_([str(item) for item in scope.tag_ids]),
                Tag.tenant_id == tenant_id,
                Tag.type == "knowledge",
                TagBinding.tenant_id == tenant_id,
            )
        )
        query = query.where(Dataset.id.in_(targets))
    return query


class MigrationRunOptions(BaseModel):
    studio_manager_url: ManagerUrl
    project_id: TargetId
    workspace_id: TargetId
    instance_id: str = Field(default="dify-1.9.1", max_length=255)
    tenant_id: UUID
    strategy: Literal["source_file", "segments"]
    dataset_ids: list[UUID] = Field(default_factory=list)
    excluded_dataset_ids: list[UUID] = Field(default_factory=list)
    source_scope: MigrationSourceScope = Field(default_factory=MigrationSourceScope)
    force: bool = False


class MigrationRunRequest(MigrationRunOptions):
    api_key: ApiKey


class MigrationRetryRequest(BaseModel):
    api_key: ApiKey


class MigrationRunConflictError(MigrationError):
    pass


def _manager_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.netloc.endswith(":")
            or parts.port == 0
            or any(char in url for char in "\\?#")
            or any(char.isspace() or not char.isprintable() for char in url)
        ):
            raise ValueError()
        parsed = AnyHttpUrl(url)
    except ValueError:
        raise MigrationError(
            "Enter a valid HTTP(S) Studio Manager base URL without credentials, query, or fragment."
        ) from None

    url = str(parsed).rstrip("/")
    host = (parsed.host or "").strip("[]").rstrip(".").lower()
    if host == "metadata.google.internal":
        raise MigrationError("Studio Manager URL cannot point to a metadata service.")
    try:
        address = ip_address(host)
    except ValueError:
        return url
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_link_local or address.is_unspecified or address.is_multicast:
        raise MigrationError("Studio Manager URL must point to a usable service address.")
    return url


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _secret_key(run_id: str) -> str:
    return f"dify:kb-migration:api-key:{run_id}"


def _log(run: DatasetMigrationRun, event: str, dataset_id: str = "", **details: str | int) -> None:
    now = _now()
    run.logs = [
        *run.logs[-(LOG_LIMIT - 1) :],
        {"time": now.isoformat() + "Z", "event": event, "dataset_id": dataset_id, **details},
    ]
    run.updated_at = now


def expire_idle_runs(tenant_id: str) -> None:
    # A worker that has stopped sending progress must not keep the target locked indefinitely.
    db.session.execute(
        update(DatasetMigrationRun)
        .where(
            DatasetMigrationRun.tenant_id == tenant_id,
            DatasetMigrationRun.status.in_(ACTIVE_STATUSES),
            DatasetMigrationRun.updated_at < _now() - IDLE_TIMEOUT,
        )
        .values(status="INTERRUPTED", updated_at=_now())
    )
    db.session.commit()


def _run_query(tenant_id: str):
    return select(DatasetMigrationRun).where(DatasetMigrationRun.tenant_id == tenant_id)


def list_runs(tenant_id: str, page: int) -> list[DatasetMigrationRun]:
    expire_idle_runs(tenant_id)
    return list(
        db.session.scalars(
            _run_query(tenant_id)
            .order_by(DatasetMigrationRun.created_at.desc(), DatasetMigrationRun.id.desc())
            .offset((page - 1) * 20)
            .limit(21)
        )
    )


def get_run(tenant_id: str, run_id: str) -> DatasetMigrationRun | None:
    expire_idle_runs(tenant_id)
    return db.session.scalar(_run_query(tenant_id).where(DatasetMigrationRun.id == run_id))


def run_response(run: DatasetMigrationRun, *, detail: bool = False) -> dict[str, object]:
    states = [row["status"] for row in run.datasets.values()]
    counts = {
        "total": len(states),
        "succeeded": states.count("SUCCEEDED"),
        "failed": states.count("FAILED"),
        "skipped": states.count("SKIPPED"),
    }
    result: dict[str, object] = {
        "id": run.id,
        "status": run.status,
        "created_at": run.created_at.isoformat() + "Z",
        "options": {key: value for key, value in run.options.items() if key != "source_account_id"},
        "counts": counts,
        "retry_of": run.retry_of,
        "can_retry": run.status not in ACTIVE_STATUSES and counts["succeeded"] + counts["skipped"] < counts["total"],
    }
    if detail:
        result.update(datasets=list(run.datasets.values()), logs=run.logs)
    return result


def retry_options(run: DatasetMigrationRun) -> MigrationRunOptions:
    unfinished = [row["id"] for row in run.datasets.values() if row["status"] not in DONE_DATASET_STATUSES]
    if run.status in ACTIVE_STATUSES or not unfinished:
        raise MigrationRunConflictError("Only failed or interrupted migrations can be retried.")
    return MigrationRunOptions.model_validate(
        {
            **run.options,
            "dataset_ids": unfinished,
            "excluded_dataset_ids": [],
            "force": False,
        }
    )


def start_run(
    options: MigrationRunOptions, api_key: str, actor: Account, retry_of: str | None = None
) -> DatasetMigrationRun:
    from tasks.dify_kb_migration_task import run_dify_kb_migration

    options.studio_manager_url = _manager_base_url(options.studio_manager_url)
    tenant_id = str(options.tenant_id)
    role = actor.current_role
    if actor.current_tenant_id != tenant_id or role not in (TenantAccountRole.ADMIN, TenantAccountRole.OWNER):
        raise MigrationError("Only a manager of the source workspace can start a migration.")
    expire_idle_runs(tenant_id)
    db.session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()).scalar_one()
    active = db.session.scalar(
        _run_query(tenant_id)
        .where(
            DatasetMigrationRun.project_id == options.project_id,
            DatasetMigrationRun.workspace_id == options.workspace_id,
            DatasetMigrationRun.status.in_(ACTIVE_STATUSES),
        )
        .limit(1)
    )
    if active is not None:
        raise MigrationRunConflictError("A migration to this target workspace is already queued or running.")

    query = console_migration_datasets(tenant_id, actor.id, role, options.source_scope).with_only_columns(
        Dataset.id, Dataset.name
    )
    if options.dataset_ids:
        query = query.where(Dataset.id.in_([str(item) for item in options.dataset_ids]))
    if options.excluded_dataset_ids:
        query = query.where(Dataset.id.not_in([str(item) for item in options.excluded_dataset_ids]))
    datasets = {
        item.id: {"id": item.id, "name": item.name, "status": "PENDING", "total": 0, "completed": 0}
        for item in db.session.execute(query)
    }
    if not datasets:
        raise MigrationError("There are no eligible knowledge bases in the selected range.")

    run = DatasetMigrationRun(
        id=str(uuid4()),
        tenant_id=tenant_id,
        project_id=options.project_id,
        workspace_id=options.workspace_id,
        status="QUEUED",
        options={**options.model_dump(mode="json"), "source_account_id": actor.id},
        datasets=datasets,
        logs=[],
        retry_of=retry_of,
    )
    _log(run, "queued")
    secret = _secret_key(run.id)
    try:
        redis_client.setex(secret, int(IDLE_TIMEOUT.total_seconds()), encrypt_token(tenant_id, api_key))
        db.session.add(run)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    try:
        run_dify_kb_migration.delay(run.id)
    except Exception:
        logger.exception("Unable to enqueue Dify knowledge migration %s", run.id)
        run.status = "FAILED"
        _log(run, "queue_failed")
        db.session.commit()
    return run


def execute_run(run_id: str) -> None:
    with Session(db.engine, expire_on_commit=False) as session:
        query = select(DatasetMigrationRun).where(DatasetMigrationRun.id == run_id).with_for_update()
        run = session.scalar(query)
        if run is None or run.status != "QUEUED":
            return
        run.status = "RUNNING"
        _log(run, "started")
        session.commit()

        def record(event: str, dataset_id: str, details: dict[str, str | int]) -> None:
            session.refresh(run, with_for_update=True)
            if run.status != "RUNNING":
                raise MigrationError("The migration run was interrupted.")
            row = {**run.datasets[dataset_id]}
            if event in DATASET_STATUSES:
                row["status"] = DATASET_STATUSES[event]
            row.update({key: details[key] for key in ("total", "completed", "target", "error") if key in details})
            run.datasets = {**run.datasets, dataset_id: row}
            _log(run, event, dataset_id, **details)
            session.commit()

        try:
            options = MigrationRunOptions.model_validate(run.options)
            account_id = run.options.get("source_account_id")
            if not account_id:
                raise MigrationError("This run predates source visibility checks. Retry it to recalculate the scope.")
            role = session.scalar(
                select(TenantAccountJoin.role).where(
                    TenantAccountJoin.tenant_id == run.tenant_id, TenantAccountJoin.account_id == account_id
                )
            )
            if role not in (TenantAccountRole.ADMIN, TenantAccountRole.OWNER):
                raise MigrationError("The migration requester no longer manages the source workspace.")
            eligible_ids = session.scalars(
                console_migration_datasets(run.tenant_id, account_id, role, options.source_scope)
                .with_only_columns(Dataset.id)
                .where(Dataset.id.in_(list(run.datasets)))
            ).all()
            base_url = _manager_base_url(options.studio_manager_url)
            secret = _secret_key(run.id)
            encrypted_key = redis_client.get(secret)
            if not encrypted_key:
                raise MigrationError("The queued credential expired. Enter the Studio API Key and retry.")
            redis_client.delete(secret)
            api_key = decrypt_token(run.tenant_id, encrypted_key.decode())
            settings = MigrationSettings.from_options(options.project_id, options.workspace_id, options.instance_id)
            client = StudioManagerClient(base_url, api_key, settings, console=True)
            with client.session:
                if eligible_ids:
                    DifyKnowledgeMigrator(
                        session,
                        client,
                        settings,
                        run.tenant_id,
                        options.strategy,
                        options.force,
                        on_event=record,
                    ).migrate(eligible_ids)
            session.refresh(run, with_for_update=True)
            if run.status != "RUNNING":
                return
            # A source can be deleted or become ineligible while the task waits for a worker.
            for dataset_id, row in list(run.datasets.items()):
                if row["status"] == "PENDING":
                    record("dataset_unavailable", dataset_id, {})
            run.status = (
                "SUCCEEDED"
                if all(row["status"] in DONE_DATASET_STATUSES for row in run.datasets.values())
                else "FAILED"
            )
            _log(run, "finished")
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("Dify knowledge migration %s stopped", run_id)
            session.refresh(run, with_for_update=True)
            if run.status == "RUNNING":
                run.status = "FAILED"
                error = str(exc) if isinstance(exc, MigrationError) else type(exc).__name__
                _log(run, "run_failed", error=error[:2000])
                session.commit()


DATASET_STATUSES = {
    "dataset_started": "RUNNING",
    "dataset_skipped": "SKIPPED",
    "dataset_succeeded": "SUCCEEDED",
    "dataset_failed": "FAILED",
    "dataset_unavailable": "FAILED",
}
