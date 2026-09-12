# Spine Web

Будущий product interface для Workspaces, Workflows, Agents, Runs и Insights.

Frontend намеренно пока не привязан к версии Next.js: сначала должны
стабилизироваться API и workflow DSL. Планируемая основа — TypeScript, React,
сгенерированный OpenAPI client, TanStack Query и SSE.

```text
app/
  overview/
  workflows/
  agents/
  runs/
  work-queue/
  insights/
  context/
  integrations/
  governance/
components/
features/
api/generated/
design-system/
```

Первый интерфейсный slice: app shell с workspace/environment, integration
health, списки agents/workflows/runs, read-only run timeline и human approval
queue. Visual workflow editor добавляется после стабилизации DSL.
