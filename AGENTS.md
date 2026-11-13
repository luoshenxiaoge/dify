# AGENTS.md

## Project Overview

Dify is an open-source platform for developing LLM applications with an intuitive interface combining agentic AI workflows, RAG pipelines, agent capabilities, and model management.

The codebase is split into:

- **Backend API** (`/api`): Python Flask application organized with Domain-Driven Design
- **Frontend Web** (`/web`): Next.js 15 application using TypeScript and React 19
- **Docker deployment** (`/docker`): Containerized deployment configurations

## Backend Workflow

- Run backend CLI commands through `uv run --project api <command>`.

- Backend QA gate requires passing `make lint`, `make type-check`, and `uv run --project api --dev dev/pytest/pytest_unit_tests.sh` before review.

- Use Makefile targets for linting and formatting; `make lint` and `make type-check` cover the required checks.

- Integration tests are CI-only and are not expected to run in the local environment.

## Frontend Workflow

```bash
cd web
pnpm lint
pnpm lint:fix
pnpm test
```

## Testing & Quality Practices

- Follow TDD: red → green → refactor.
- Use `pytest` for backend tests with Arrange-Act-Assert structure.
- Enforce strong typing; avoid `Any` and prefer explicit type annotations.
- Write self-documenting code; only add comments that explain intent.

## Language Style

- **Python**: Keep type hints on functions and attributes, and implement relevant special methods (e.g., `__repr__`, `__str__`).
- **TypeScript**: Use the strict config, lean on ESLint + Prettier workflows, and avoid `any` types.

## General Practices

- Prefer editing existing files; add new documentation only when requested.
- Inject dependencies through constructors and preserve clean architecture boundaries.
- Handle errors with domain-specific exceptions at the correct layer.

## Project Conventions

- Backend architecture adheres to DDD and Clean Architecture principles.
- Async work runs through Celery with Redis as the broker.
- Frontend user-facing strings must use `web/i18n/en-US/`; avoid hardcoded text.

## 用户约定的迁移开发与上线导出规则

- Dify 功能先修改并验证 D:/PycharmProjects/dify-1104，再同步到生产源码 D:/PycharmProjects/dify_tm/dify/ai_agent_api。
- 迁移专用逻辑放在迁移模块内。普通知识库列表及权限筛选等原有业务逻辑须保留。
- 只有用户明确说“开始上线打包”时，才把累计改动文件导出到桌面。日常开发和同步完成后，记录本批次改动即可。
- 每次打包导出自上一次“开始上线打包”到当前的累计有效改动。确认导出成功后，再开启下一批次。已经回退的文件不计入交付。
- 首批累计起点为用户要求“改下查该空间下需要迁移的知识库的逻辑……一切以我在前端界面看到知识库为准……”的那次修改。
- 当前批次清单保存在 C:/tools/dify-migration-release/current-batch.json。后续每次修改迁移功能都更新该清单，打包前核对最终差异。部署源码按原项目相对路径导出。
