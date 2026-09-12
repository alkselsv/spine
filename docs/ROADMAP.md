# Roadmap — Spine

Статус: рабочий план продуктовых релизов.

Приоритет определяется пользовательскими кейсами, а не очередностью строительства
технических слоёв:

1. Q&A по внутренним документам.
2. Генерация коммерческих предложений.
3. Мониторинг клиентских коммуникаций.
4. Контроль исполнения внутренних задач.

Каждый релиз является вертикальным срезом и включает необходимые ему источники,
domain contracts, agents/code steps, workflow, evaluations, API, интерфейс,
наблюдаемость и эксплуатационные ограничения. Общие компоненты извлекаются в
платформу по мере подтверждения минимум двумя кейсами.

## R0 — Общая основа

Цель: подготовить минимальный фундамент, не строя заранее универсальную agent
platform.

- [x] Python/FastAPI-проект и локальный Q&A-прототип на Cognee.
- [x] Каркас domain, application, connectors, memory, agents, workflows и web.
- [x] Framework-independent контракты capability, agent invocation, artifact и workflow.
- [ ] Workspace и environment context во всех API-командах.
- [ ] `SourceObject`, `SourceRevision`, immutable originals и provenance locators.
- [ ] Context Broker с Cognee как заменяемой retrieval-проекцией.
- [ ] Единые trace ID, structured errors и audit events.
- [ ] Минимальный app shell и сгенерированный API client.

R0 не является самостоятельным продуктовым релизом: задачи из него выполняются
по мере необходимости для R1.

## R1 — Q&A по внутренним документам

### Пользовательский результат

Сотрудник загружает или подключает разрешённые ему документы, задаёт вопрос в
web-интерфейсе и получает проверяемый ответ со ссылками на конкретные фрагменты
актуальных версий источников.

### Scope

- [ ] Надёжная загрузка PDF, DOCX, XLSX, Markdown и text с версиями и checksum.
- [ ] Parsing metadata: страницы, листы, строки, заголовки и другие stable locators.
- [ ] Индексация, обновление, удаление и полная перестройка Cognee-проекции.
- [ ] Object-level ACL и наследование прав документ → chunk/reference.
- [ ] Typed retrieval contract через Context Broker.
- [ ] `/api/v1/ask`: answer, citations, confidence, `as_of`, trace ID и abstention.
- [ ] Q&A/Search UI: выбор scope, citations inspector, freshness и feedback.
- [ ] Golden dataset из реальных обезличенных вопросов.
- [ ] Regression eval для retrieval, groundedness, citation correctness и ACL leakage.
- [ ] Метрики latency, cost, answer rate и пользовательской полезности.

### Exit criteria

- Ответ нельзя выдать без валидной ссылки на доступную ревизию источника.
- Недостаточный контекст приводит к явному отказу или уточняющему вопросу.
- Изменение/удаление документа отражается в результатах после контролируемого sync.
- Пользователь не может получить сведения из недоступного ему документа.
- Согласованные quality thresholds проходят на versioned eval dataset.
- Основной сценарий выполняется через UI без CLI.

### Не входит

- Диалоговый универсальный copilot с внешними действиями.
- Визуальный workflow builder.
- Полная платформа сторонних агентов.

## R2 — Генерация коммерческих предложений

### Пользовательский результат

Менеджер передаёт первичный запрос и материалы, получает найденные исторические
аналоги, уточняющие вопросы и редактируемый черновик КП/сметы с прозрачным
происхождением каждой существенной позиции.

### Scope

- [ ] Google Sheets connector для реестра смет и Google Drive/file connector для материалов.
- [ ] Связывание строки реестра, запроса, встречи, клиента и итоговой сметы.
- [ ] Domain model: `ClientRequest`, `Requirement`, `ProposalDraft`, `Estimate`,
      `EstimateLine`, `Assumption`, `Exclusion`, `PricingBasis` и template version.
- [ ] Capability steps: классификация запроса, извлечение требований, поиск аналогов,
      подготовка структуры и генерация draft.
- [ ] Hybrid retrieval по структурированным признакам и семантической близости.
- [ ] Детерминированные вычисления стоимости, валюты, налогов и округления.
- [ ] Completeness gate и итеративный clarification loop с менеджером.
- [ ] Human review/edit/approve с version diff и optimistic locking.
- [ ] Редактор сметы и КП, evidence/analogue inspector, preview и экспорт.
- [ ] Eval dataset для извлечения требований, выбора аналогов и состава сметы.
- [ ] Метрики времени подготовки, размера человеческой правки и ошибок оценки.

### Exit criteria

- Черновик содержит источник для каждого использованного аналога и pricing basis.
- Итоговые суммы воспроизводятся детерминированно и не вычисляются LLM.
- При недостатке данных workflow приостанавливается и запрашивает уточнение.
- Ни один документ не отправляется клиенту без явного решения менеджера.
- Сохраняются исходный draft, правки и версия, утверждённая для отправки.
- Согласованные quality и time-saving thresholds проходят на исторических кейсах.

### Не входит

- Автоматическая отправка КП клиенту.
- Автоматическое изменение прайс-листов.
- Универсальный редактор документов любого типа.

## R3 — Мониторинг клиентских коммуникаций

### Пользовательский результат

Ответственный видит единую очередь клиентских ситуаций, требующих реакции:
вопросы без ответа, обязательства, запросы документов, задержки и признаки
недовольства — с приоритетом, evidence и предлагаемым следующим действием.

### Scope

- [ ] Telegram connector, затем MAX connector, с incremental sync и revisions.
- [ ] Canonical model: `Conversation`, `Thread`, `Participant`, `MessageRevision`,
      `Question`, `Commitment` и связи с клиентом/проектом.
- [ ] Cross-source identity linking с проектными документами и минимальной CRM-проекцией.
- [ ] Versioned detectors: unanswered, commitment, document request, complaint и SLA risk.
- [ ] Finding lifecycle: open, acknowledged, snoozed, resolved и reopened.
- [ ] Deduplication, repeated observation, suppression/cooldown и priority policy.
- [ ] Настраиваемые recipients, cadence, digest, quiet hours и delivery channels.
- [ ] Findings Inbox, conversation timeline, evidence и feedback actions в UI.
- [ ] Исторический replay/backtest detectors до production-включения.
- [ ] Метрики precision/recall, false-positive rate, time-to-detect и response time.

### Exit criteria

- Повторный анализ не создаёт дубликаты одной и той же ситуации.
- Для finding доступны сообщения-основания и объяснение приоритета.
- Закрытая ситуация переоткрывается только при новом релевантном событии.
- Уведомления соблюдают routing, cadence, quiet hours и idempotency.
- Менеджер может подтвердить, отклонить, отложить и закрыть finding через UI.
- Согласованные detector thresholds проходят на исторической временной линии.

### Не входит

- Автоматические ответы клиенту.
- Автоматическое создание задач без approval/policy.
- Анализ всех корпоративных каналов одновременно.

## R4 — Контроль исполнения внутренних задач

### Пользовательский результат

Менеджеры и руководители видят задачи, которые фактически остановились, причину
остановки, ожидаемого сотрудника или подразделение и рекомендуемое действие —
даже если формальный deadline ещё не нарушен.

### Scope

- [ ] Полный Bitrix24 connector для задач, комментариев, участников и статусов.
- [ ] Canonical task timeline и связь task ↔ conversation ↔ project ↔ people/team.
- [ ] Business calendars, рабочее время, SLA policies и grace periods.
- [ ] Detectors: unanswered internal question, missing input, blocked progress,
      delayed reaction, waiting-on и management intervention.
- [ ] Responsibility resolution: assignee, mentioned participant, team и escalation chain.
- [ ] Event-time semantics, late events и пересчёт findings после изменений задним числом.
- [ ] Management overview, task timeline, blocker evidence и responsibility view.
- [ ] Эскалации, digest и контролируемое создание follow-up задач/напоминаний.
- [ ] Replay/eval на исторических задачах и feedback руководителей.
- [ ] Метрики precision/recall, blocked duration, intervention time и resolved blockers.

### Exit criteria

- Система различает просроченную задачу и фактическую смысловую блокировку.
- Причина, ожидаемая сторона и evidence доступны для каждого finding.
- SLA учитывает календарь, часовой пояс и поступившие задним числом события.
- Эскалация не дублируется и следует настраиваемой организационной цепочке.
- Внешнее изменение задачи выполняется идемпотентно и только по policy/approval.
- Согласованные detector thresholds проходят на исторических задачах.

### Не входит

- Автономное управление сотрудниками или оценка их эффективности.
- Автоматическое закрытие и переназначение задач без утверждённой policy.
- Универсальный process-mining всех процессов компании.

## R5 — Платформенное обобщение и масштабирование

Этот этап начинается после подтверждения общих abstractions четырьмя кейсами.

- [ ] Унифицированные workflow deployments, reusable subworkflows и visual editor.
- [ ] Agent catalog UI, version comparison, shadow/canary и rollback.
- [ ] Tool registry, scoped grants, service identities и kill switch.
- [ ] Third-party Agent SDK, manifests и certification — при наличии внешних integrations.
- [ ] Cross-case Insights: cost, quality, human correction и business outcomes.
- [ ] Continuous evaluation и drift monitoring.
- [ ] Process mining и объяснимые automation opportunities.
- [ ] Независимое масштабирование workers и production graph/vector backend по нагрузке.

## Сквозные правила поставки

Для каждого релиза обязательны:

- versioned schemas, prompts, detectors, agents и eval datasets;
- provenance и evidence для выводов, влияющих на решение или действие;
- workspace isolation, least privilege и audit trail;
- idempotency для ingestion, notifications и внешних действий;
- API и UI одного и того же vertical slice;
- offline evaluation до включения и feedback loop после включения;
- rollback/reindex/replay procedure;
- измеримый пользовательский и бизнес-результат.

## Общие non-goals до завершения R4

- Marketplace агентов.
- Автоматическое создание production-workflow из plain English.
- Универсальный no-code BPM-конструктор.
- Неограниченные автономные внешние действия.
- Kafka и преждевременное микросервисное разбиение.
