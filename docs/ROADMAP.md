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

## Статусы поставки

Roadmap различает три статуса. Они относятся только к явно названной границе и
не должны автоматически переноситься на весь релиз:

- `implemented` — контракт или компонент реализован и покрыт проверками на своей
  границе;
- `integrated` — компонент подключён к целевому пользовательскому пути релиза, а
  не только к fake, прототипу или compatibility path;
- `qualified` — для названной границы собраны acceptance evidence и пройдены её
  обязательные gates.

`qualified` component contract не означает production qualification внешнего
провайдера или завершение релиза. Чекбокс `[x]` означает, что завершён именно
сформулированный scope item; промежуточное состояние незавершённых пунктов
указывается отдельными status notes.

## R0 — Общая основа

Цель: подготовить минимальный фундамент, не строя заранее универсальную agent
platform.

- [x] Python/FastAPI-проект и локальный Q&A-прототип на Cognee.
      `implemented`, `operational: legacy compatibility path`,
      `not integrated: governed R1 flow`.
- [x] Каркас domain, application, connectors, memory, agents, workflows и web.
      `implemented`.
- [x] Начальный framework-independent каркас контрактов capability, agent invocation, artifact и workflow.
      `implemented`.
- [x] Local agent runtime seam: capability-specific schemas, типизированные
      `AgentRequest`/`AgentResponse` и полный `AgentExecutionContext`.
      `implemented`, `qualified: deterministic offline contract`,
      `integrated: R1 candidate generation and typed artifacts`,
      `integrated and qualified: deployed R1 AnswerQuestionCapability composition`
      в Q16-01 и governed browser flow.
- [x] Явный registry/factory, structural `AgentHandler`, adapter async-функций,
      `EventSink`, cooperative cancellation и разделение runtime/workflow retry.
      `implemented`, `qualified: deterministic offline contract`,
      `integrated and qualified: durable deployed R1 Ask lifecycle через общий
      API / Temporal / observer path`; Tool Gateway остаётся вне R1 scope.
- [ ] Workspace и environment context во всех API-командах.
      `implemented and qualified: identity/request/persistence foundation`;
      `integrated: /api/v1 control-plane and Q&A resources`;
      `not integrated by design: legacy /ask and /ingest compatibility path`.
- [x] `SourceObject`, `SourceRevision`, immutable originals и provenance locators.
      `implemented, integrated and qualified`: deployed five-format canonical
      ingestion, version/replacement/tombstone lifecycle и stable locators прошли
      Q16-02 R4 (#561).
- [x] Context Broker с Cognee как заменяемой retrieval-проекцией.
      `implemented: provider-neutral projection and authorized retrieval contracts`;
      `integrated and qualified`: deterministic/offline contracts и configured
      Cognee 1.5.4 + PostgreSQL 17 + Neo4j + PGVector profile; все physical gates,
      включая canonical lifecycle separation, прошли (#212, #507, #562).
- [x] Единые trace ID, structured errors и audit events.
      `implemented`, `qualified: offline diagnostics/delivery foundation`,
      `integrated: API, outbox, workers, projection, Q&A and no-log diagnosis`;
      `not qualified: complete production deployment`.
- [x] Минимальный app shell и сгенерированный API client.
      `implemented and qualified: reproducible OpenAPI generation, scoped typed
      client and TanStack Query boundary`; R1 Knowledge, Chat and Runs resources
      прошли controlled и deployed governed browser qualification (#508, #560).

R0 не является самостоятельным продуктовым релизом: задачи из него выполняются
по мере необходимости для R1.

## R1 — Q&A по внутренним документам

### Пользовательский результат

Администратор загружает разрешённые документы, наблюдает их обработку и через
диагностический Q&A проверяет retrieval и generation в web-интерфейсе. Каждый
выданный ответ прослеживается до конкретных фрагментов актуальных версий
источников и связанного run; ошибки ingestion, projection и generation
локализуются без CLI.

### Scope

Основной governed flow `upload → projection → answer → citations → UI` собран
одной production-capable композицией и квалифицирован на изолированном локальном
deployment profile: runtime readiness (#560), пятиформатный canonical ingestion
(#561), physical projection publication/recovery (#562) и реальный browser-flow
без API mocking (#508) имеют `PASS`. Это не завершает релиз: approved private
Golden Dataset ещё не прогнан через тот же production Q&A path, численные quality
thresholds не утверждены, recovered application canary не квалифицирован, а
финальный current-commit evidence aggregate и GO/NO-GO отсутствуют (#510, #511,
#634, #16).

- [x] Надёжная загрузка PDF, DOCX, XLSX, Markdown и text с версиями и checksum.
      `implemented, integrated and qualified`: Q16-02 R4 прошёл deployed upload,
      duplicate replay, changed revision, tombstone и retry после parser,
      original-storage и pre-acceptance failures (#561, #570).
- [x] Parsing metadata: страницы, листы, строки, заголовки и другие stable locators.
      `implemented, integrated and qualified` для всех R1-форматов; форматные
      locators сохранены через canonical ingestion и projection handoff (#561,
      #565, #568, #581).
- [x] Versioned baseline ontology и evidence-linked извлечение domain entities и
      relations из `SourceRevision`.
      `implemented, integrated and qualified`: live structured chunk/entity/
      relation round trip сохраняет exact SourceRevision provenance на approved
      physical provider profile (#212, #507, #562).
- [x] Индексация, обновление, удаление и полная перестройка graph/vector Context
      projection; Cognee — первый replaceable adapter.
      `implemented, integrated and qualified`: physical Cognee / Neo4j / PGVector
      profile, shadow validation, activation CAS, update/remove/tombstone,
      injected publication failures, redrive и recovery successor прошли (#212,
      #562, #576–#583).
- [x] Object-level Access Policy и наследование прав документ → chunk/reference.
      `implemented, integrated and qualified`: canonical policy, human/service
      intersection, RLS, current authorization, authorized retrieval и disclosure
      revalidation прошли deployed runtime и two-workspace browser scenarios
      (#508, #544, #560).
- [x] Typed retrieval contract через Context Broker.
      `implemented, integrated and qualified`: authorized retrieval, immutable
      ContextBundle, exact provenance, replay и physical-provider execution
      проверены в общем production Q&A path (#508, #560, #562).
- [x] Capability `answer_question` как эталонный local `AgentHandler`: pinned
      `AgentVersion`/`ContextProfile`, runtime-managed artifacts и citations.
      `implemented, integrated and qualified`: shared API / Temporal / observer
      composition, authenticated same-execution capture и production model/provider
      bindings прошли Q16-01 и governed browser qualification (#469, #508, #560).
- [x] `/api/v1/ask`: grounded answer/outcome, structured citations, optional
      versioned confidence metadata, `as_of`, trace ID и abstention.
      `implemented, integrated and qualified`: durable admission/status/
      cancellation, SSE replay, sessions, structured citations, abstention,
      terminal persistence и disclosure rejection прошли deployed flow (#508,
      #560).
- [x] Administrator-only Knowledge Control Plane: документы и ревизии, ingestion /
      projection status, диагностический Q&A, citations inspector, run timeline,
      freshness и feedback.
      `implemented, integrated and qualified`: generated client, components,
      controlled Playwright и real Chromium journey против deployed API/provider;
      основной сценарий выполняется без CLI (#508, #560, #561).
- [ ] Golden Dataset из реальных корпоративных документов и вопросов для R1 Q&A;
      обязательная анонимизация не требуется. Приватный корпус используется
      только в пределах разрешения и действующих runtime authorization и
      AccessPolicy, доступ к нему ограничен, он хранится вне Git; Git и CI
      используют только безопасные synthetic или anonymized fixtures.
      `implemented: approved private baseline 0.3.9-user-approved, 30 sources and
      149 cases`; mapping, canonical preparation and post-observation evaluation
      contracts реализованы; полный 149-case production-flow run не выполнен,
      direct denied coverage в dataset отсутствует (#510).
- [ ] Regression eval для retrieval, groundedness, citation correctness и access-policy leakage.
      `implemented and qualified: deterministic synthetic offline gate`;
      real-observation evaluator реализован, end-to-end benchmark не выполнен.
- [ ] Метрики latency, cost, answer rate и пользовательской полезности.
      `implemented and qualified: versioned deterministic operational read model`;
      real-run measurements, deployment retention/bindings и release thresholds
      ещё не подтверждены.

### Актуальные release blockers

- #634 — provider-backed projection publication и один authorized Context Broker
  canary на fresh recovered target. Run007 выполнил ровно один production
  projection consumer invocation и подтвердил receipt, finalized snapshot,
  activation, active publication, reconciliation и source-revision lineage для
  synthetic recovery data. Broker не вызывался: pre-Broker проверка обнаружила,
  что production evidence verifier требовал interactive authority вместо
  server-only noninteractive path. Mode-aware evidence authorization,
  one-shot qualified Broker boundary и единая Broker-only persistence authority
  реализованы и покрыты offline-тестами, но после этих изменений fresh canary не
  выполнялся. Нужны новый отдельно авторизованный run, ровно один Broker request
  с Context Bundle evidence и independent evidence review.
- #510 — полный 149-case run approved Golden Dataset через production durable
  Q&A/capture path, metadata-only baseline и отдельное утверждение versioned
  numerical thresholds Product Owner-ом.
- #511 — current-commit aggregate всех обязательных R0/R1 evidence, repository
  gates, legacy disposition и финальное воспроизводимое решение GO/NO-GO.
- #16 — родительский end-to-end release gate остаётся открытым до завершения
  #510, #511 и #634; закрытые component tickets сами по себе не означают
  готовность R1.

### Текущий release verdict

`NO-GO`: продуктовый R1 путь реализован и квалифицирован на зафиксированных
изолированных deployment targets, но release qualification ещё не завершена.
Отсутствие завершённого recovered-application canary, реального
baseline/approved thresholds или единого совместимого evidence package является
блокирующим, а не неявным `PASS`. Физический restore/recovery basis уже проверен;
не закрыта provider-backed проверка приложения и Context Broker на восстановленном
target.

### Exit criteria

- Ответ нельзя выдать без валидной ссылки на доступную ревизию источника.
- Недостаточный контекст приводит к явному отказу или уточняющему вопросу.
- Изменение/удаление документа отражается в результатах после контролируемого sync.
- Извлечённые entities и relations ссылаются на конкретные source revisions и
  перестраиваются вместе с versioned Context Graph.
- Пользователь не может получить сведения из недоступного ему документа.
- Каждый Q&A run сохраняет workspace/environment, acting subject, закреплённые
  версии agent/prompt/context, input/output artifacts и единый trace ID.
- Согласованные quality thresholds проходят на versioned eval dataset.
- Администратор выполняет основной сценарий загрузки, диагностики и контрольного
  Q&A через UI без CLI.

### Не входит

- Диалоговый универсальный copilot с внешними действиями.
- Q&A-интерфейс для обычных сотрудников.
- Визуальный workflow builder.
- Полная платформа сторонних агентов.

## R2 — Генерация коммерческих предложений

### Пользовательский результат

Менеджер передаёт первичный запрос и материалы, получает найденные исторические
аналоги, уточняющие вопросы и редактируемый черновик КП/сметы с прозрачным
происхождением каждой существенной позиции.

### Scope

- [x] Канонический разговорный запуск: idempotent `WorkItem` и pinned
      `WorkflowRun`, транзакционный outbox, scoped readback и composer (#638).
- [x] Продолжение того же `WorkflowRun`: typed `add_context` command, optimistic
      command receipts, disclosure-safe snapshot/event feed с stable cursor и
      восстановление минимальной web timeline после reload (#639).
- [ ] Google Sheets connector для реестра смет и Google Drive/file connector для материалов.
      `implemented: concrete governed Drive v3 / Sheets v4 REST and Desktop OAuth
      reference adapters with deterministic offline qualification (#652)`;
      `implemented: provider-neutral connection configuration and safe operator
      inspection through PostgreSQL, /api/v1 and product UI with fake-provider
      qualification (#664)`;
      `implemented: bounded provider-neutral Yandex Disk file reader with
      deterministic offline qualification (#665)`;
      `implemented: bounded provider-neutral Bitrix24 Disk file reader with
      deterministic offline qualification (#666)`;
      `implemented: governed Bitrix24 CRM activity-attachment material claims
      with conditional CRM capability, exact current Disk revision acceptance,
      revocation evidence and deterministic offline qualification (#671)`;
      `implemented: conservative Yandex Disk full-scope reconciliation with
      confirmed scan generations, approval-bound removal policy, opaque durable
      checkpoints, path-scoped reappearance and fail-closed lifecycle evidence
      (#670)`;
      `implemented: governed mounted-filesystem import and conservative change
      reconciliation with verified scan generations, unavailable/incomplete
      fencing, path-scoped rename/reappearance and unstable-read quarantine
      (#667, #672)`; `accepted: pre-mounted read-only production policy,
      ownership and qualification gate (#659)`; `not qualified: selected real
      mounted-filesystem backend (#680)`;
      `not qualified: live Google and Shared Drive behavior`; product integration
      remains open.
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
- [ ] Third-party Agent SDK, manifests и certification — только при появлении
      подтверждённого продуктового кейса для внешних агентов.
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
