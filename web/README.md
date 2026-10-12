# Spine Web

Первый product interface — администраторская Control Plane для управления
Company Brain: загрузка документов, контроль ingestion/projection, проверочный
Q&A Chat с citations и диагностика runs. Полные требования описаны в
[`docs/INTERFACE.md`](../docs/INTERFACE.md).

Выбранный стек R1: TypeScript strict, React SPA, Vite, React Router Data Mode,
Base UI, CSS variables + CSS Modules, TanStack Query, сгенерированный OpenAPI
client и SSE. Package manager — `pnpm`; тесты — Vitest, Testing Library и
Playwright. FastAPI остаётся единственным backend/control-plane server.

Визуальное направление первого среза задают выбранные и адаптированные паттерны
Halaska UI. Chat использует Spine-owned transport поверх API/SSE. Эти решения и
условия их проверки описаны в
[`docs/INTERFACE.md`](../docs/INTERFACE.md#8-решения-по-реализации-интерфейса).

```text
app/
  knowledge/
  chat/
  runs/
  settings/
components/
features/
api/generated/
design-system/
```

Первый интерфейсный slice доступен только администратору. Он замыкает путь
`upload → parsing/indexing → test question → citations → run diagnostics`.
Chat для сотрудников, agent/workflow catalog, approvals, Insights и visual
workflow editor добавляются последующими вертикальными срезами.

## Локальная разработка

Требуется Node.js `>=22.12.0`. Версия pnpm закреплена в корневом
`package.json` и `pnpm-lock.yaml`; включите Corepack перед первой установкой:

```bash
corepack enable
pnpm install --frozen-lockfile
```

Команды запускаются из корня репозитория:

```bash
pnpm web:dev       # Vite development server
pnpm web:typecheck # strict TypeScript
pnpm web:build     # static SPA production build
pnpm web:test      # Vitest + Testing Library
pnpm web:test:e2e  # Playwright against a controlled Vite preview
pnpm web:check     # all frontend checks in sequence
```

Playwright в CI устанавливает Chromium вместе с системными зависимостями. Для
локального запуска browser smoke-теста на Linux установите их командой:

```bash
pnpm --dir web exec playwright install --with-deps chromium
```

`web/api/generated/` зарезервирован для generated OpenAPI client и не изменяется
вручную. Frontend не обращается к базе, Cognee, model provider или отдельному
Node BFF: FastAPI остаётся единственным backend/control-plane server.
