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
