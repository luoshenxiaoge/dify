from contextvars import copy_context
from threading import Lock

from celery import shared_task

import contexts
from services.dify_kb_migration_runs import execute_run


def _execute_with_workspace_models(run_id: str) -> None:
    # Keep provider data scoped to this run and initialize each cache with its lock.
    contexts.plugin_model_providers_lock.set(Lock())
    contexts.plugin_model_providers.set(None)
    contexts.plugin_model_schema_lock.set(Lock())
    contexts.plugin_model_schemas.set({})
    execute_run(run_id)


@shared_task(queue="dataset", ignore_result=True)
def run_dify_kb_migration(run_id: str) -> None:
    copy_context().run(_execute_with_workspace_models, run_id)
