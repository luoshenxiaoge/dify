from flask import request
from flask_login import current_user
from flask_restx import Resource
from pydantic import ValidationError
from sqlalchemy import func, select
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

from controllers.console import console_ns
from controllers.console.wraps import account_initialization_required, enterprise_license_required, setup_required
from extensions.ext_database import db
from libs.login import login_required
from models.dataset import Dataset
from services.dify_kb_migration import MigrationError
from services.dify_kb_migration_runs import (
    MigrationRetryRequest,
    MigrationRunConflictError,
    MigrationRunOptions,
    MigrationRunRequest,
    MigrationSourceScope,
    console_migration_datasets,
    get_run,
    list_runs,
    retry_options,
    run_response,
    start_run,
)


def _tenant_id() -> str:
    if not current_user.is_admin_or_owner or not current_user.current_tenant_id:
        raise Forbidden()
    return current_user.current_tenant_id


def _page() -> int:
    return max(1, request.args.get("page", default=1, type=int))


@console_ns.route("/datasets/migrations/sources")
class DatasetMigrationSourcesApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @enterprise_license_required
    def get(self):
        tenant_id = _tenant_id()
        try:
            scope = MigrationSourceScope.model_validate(
                {
                    "include_all": request.args.get("include_all", "false").lower() == "true",
                    "keyword": request.args.get("source_keyword", ""),
                    "tag_ids": request.args.getlist("tag_ids"),
                }
            )
        except ValidationError:
            raise BadRequest("Invalid source knowledge base filters.") from None
        query = console_migration_datasets(tenant_id, current_user.id, current_user.current_role, scope)
        scope_total = db.session.scalar(
            select(func.count()).select_from(query.with_only_columns(Dataset.id).order_by(None).subquery())
        )
        query = query.with_only_columns(Dataset.id, Dataset.name)
        search = request.args.get("keyword", "")[:255].strip()
        if search:
            query = query.where(Dataset.name.icontains(search, autoescape=True))
        total = (
            db.session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
            if search
            else scope_total
        )
        rows = db.session.execute(query.offset((_page() - 1) * 20).limit(21)).all()
        return {
            "data": [{"id": item.id, "name": item.name} for item in rows[:20]],
            "has_more": len(rows) > 20,
            "total": total,
            "scope_total": scope_total,
        }


@console_ns.route("/datasets/migrations")
class DatasetMigrationRunsApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @enterprise_license_required
    def get(self):
        runs = list_runs(_tenant_id(), _page())
        return {"data": [run_response(run) for run in runs[:20]], "has_more": len(runs) > 20}

    @setup_required
    @login_required
    @account_initialization_required
    @enterprise_license_required
    def post(self):
        tenant_id = _tenant_id()
        try:
            payload = MigrationRunRequest.model_validate(request.get_json())
        except ValidationError:
            raise BadRequest("Invalid migration options.") from None
        if str(payload.tenant_id) != tenant_id:
            raise Forbidden()
        options = MigrationRunOptions.model_validate(payload.model_dump(exclude={"api_key"}))
        try:
            run = start_run(options, payload.api_key, current_user)
        except MigrationRunConflictError as exc:
            raise Conflict(str(exc)) from None
        except MigrationError as exc:
            raise BadRequest(str(exc)) from None
        return run_response(run, detail=True), 202


@console_ns.route("/datasets/migrations/<uuid:run_id>")
class DatasetMigrationRunApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @enterprise_license_required
    def get(self, run_id):
        run = get_run(_tenant_id(), str(run_id))
        if run is None:
            raise NotFound()
        return run_response(run, detail=True)


@console_ns.route("/datasets/migrations/<uuid:run_id>/retry")
class DatasetMigrationRetryApi(Resource):
    @setup_required
    @login_required
    @account_initialization_required
    @enterprise_license_required
    def post(self, run_id):
        run = get_run(_tenant_id(), str(run_id))
        if run is None:
            raise NotFound()
        try:
            payload = MigrationRetryRequest.model_validate(request.get_json())
        except ValidationError:
            raise BadRequest("Enter a Studio knowledge API key.") from None
        try:
            retried = start_run(retry_options(run), payload.api_key, current_user, retry_of=run.id)
        except MigrationRunConflictError as exc:
            raise Conflict(str(exc)) from None
        except MigrationError as exc:
            raise BadRequest(str(exc)) from None
        return run_response(retried, detail=True), 202
