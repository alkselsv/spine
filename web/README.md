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
Route shell, URL scope context, administrator boundary, localized states and
reference decisions реализованы в
[`docs/design/issue-165-control-plane-shell.md`](../docs/design/issue-165-control-plane-shell.md).

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
pnpm web:api:generate # export FastAPI OpenAPI and regenerate TypeScript types
pnpm web:api:check # regenerate and fail if committed artifacts differ
pnpm web:typecheck # strict TypeScript
pnpm web:build     # static SPA production build
pnpm web:test      # Vitest + Testing Library
pnpm web:test:e2e  # Playwright against a controlled Vite preview
pnpm --dir web test:e2e:production # opt-in deployed-backend qualification
pnpm web:check     # all frontend checks in sequence
```

Playwright в CI устанавливает Chromium вместе с системными зависимостями. Для
локального запуска browser smoke-теста на Linux установите их командой:

```bash
pnpm --dir web exec playwright install --with-deps chromium
```

`web/api/openapi.json` и `web/api/generated/` создаются только командой
`pnpm web:api:generate` и не изменяются вручную. Typed runtime boundary находится
в `web/src/api/`: он получает bearer credential и Workspace/Environment scope
через injection, безопасно отображает structured errors и передаёт server state
в TanStack Query. Frontend не обращается к базе, Cognee, model provider или
отдельному Node BFF: FastAPI остаётся единственным backend/control-plane server.

## Browser qualification

`pnpm web:test:e2e` запускает собранную SPA в Chromium. Тестовые URL с
`fixture=controlled` включают только синтетический API/SSE boundary для
квалификации foundation; этот режим не является авторизацией и не считается production evidence.
Опциональная `test:e2e:production` команда работает только с уже развёрнутым
Web/API. Она загружает runtime identity/scope из корневого `.env`, а approved
browser inputs и evidence paths — из `.env.e2e`; команда не поднимает preview
server и не перехватывает API.
Описание контролируемых сценариев находится в
[`docs/design/issue-167-frontend-qualification.md`](../docs/design/issue-167-frontend-qualification.md).
Живая R1-квалификация и формат metadata-only отчёта описаны в
[`docs/runbooks/r1-governed-qa-browser-qualification.md`](../docs/runbooks/r1-governed-qa-browser-qualification.md).
