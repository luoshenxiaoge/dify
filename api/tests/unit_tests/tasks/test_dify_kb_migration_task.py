from contextvars import Context
from unittest.mock import Mock

import pytest

import contexts
from core.model_runtime.entities.model_entities import AIModelEntity, ModelType
from core.model_runtime.model_providers.model_provider_factory import ModelProviderFactory
from tasks import dify_kb_migration_task


def test_migration_worker_isolates_plugin_model_cache_and_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = ModelProviderFactory("workspace")
    plugin_client = Mock()
    plugin_client.fetch_model_providers.return_value = []
    schema = Mock(spec=AIModelEntity)
    plugin_client.get_model_schema.return_value = schema
    monkeypatch.setattr(factory, "plugin_model_manager", plugin_client)

    def execute_run(run_id: str) -> None:
        assert run_id == "migration-run"
        assert factory.get_plugin_model_providers() == []
        assert (
            factory.get_model_schema(
                provider="langgenius/openai_api_compatible/openai_api_compatible",
                model_type=ModelType.TEXT_EMBEDDING,
                model="Bge-m3-test",
                credentials={},
            )
            is schema
        )

    monkeypatch.setattr(dify_kb_migration_task, "execute_run", execute_run)

    def run_with_inherited_caches() -> None:
        # The production worker previously seeded caches without their matching locks.
        contexts.plugin_model_providers.set(None)
        parent_schemas: dict[str, AIModelEntity] = {}
        contexts.plugin_model_schemas.set(parent_schemas)

        dify_kb_migration_task.run_dify_kb_migration.run("migration-run")
        dify_kb_migration_task.run_dify_kb_migration.run("migration-run")

        assert contexts.plugin_model_providers.get() is None
        assert contexts.plugin_model_schemas.get() is parent_schemas
        assert not parent_schemas

    Context().run(run_with_inherited_caches)
    assert plugin_client.fetch_model_providers.call_count == 2
    assert plugin_client.get_model_schema.call_count == 2
