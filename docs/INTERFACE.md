# Интерфейс Spine

Статус: **рабочие продуктовые требования**  
Область: первый интерфейсный срез R1 — администраторская Control Plane для
управления Company Brain и проверки Q&A.

## 1. Назначение первого интерфейса

Первый интерфейс Spine предназначен для администратора. Он загружает документы,
наблюдает их обработку и проверяет качество Company Brain через встроенный чат.
Чат в R1 является диагностической консолью, а не пользовательским корпоративным
ассистентом: доступ к нему имеет только администратор.

Интерфейс должен замыкать один проверяемый цикл:

```text
загрузить документ
  → дождаться parsing и indexing
  → проверить разрешённое содержимое и состояние проекции
  → задать контрольный вопрос
  → проверить ответ, abstention и citations
  → открыть run и локализовать проблему
  → заменить источник или запустить повторную обработку
```

Продуктовым и визуальным референсом служит
[Agno Control Plane](https://www.agno.com/products/control-plane): прежде всего
его представления Knowledge, Chat, Sessions и Tracing. Spine не принимает Agno
как runtime-зависимость и не копирует его browser-to-runtime архитектуру. Web UI
обращается к API Spine; браузер не работает напрямую с Cognee, model provider,
Temporal или хранилищами.

## 2. Пользователь и права

В R1 поддерживается одна интерфейсная роль — `administrator`.

Администратор может:

- загружать поддерживаемые документы в workspace;
- видеть состояние parsing, projection и indexing;
- просматривать safe operational metadata и sanitized diagnostics;
- при наличии `read_content` просматривать protected metadata, ревизии,
  извлечённое содержимое, citations и evidence;
- логически удалять документ и запускать повторную обработку;
- задавать вопросы встроенному capability `answer_question`;
- просматривать разрешённую диагностическую информацию run;
- оставлять feedback на ответ и citations.

Обычные сотрудники не получают доступ к чату или панели в первом релизе. Это не
отменяет серверную модель авторизации: API проверяет роль, `workspace_id` и
environment для каждой команды и retrieval request. Скрытая навигация не
считается контролем доступа. Роль `administrator` не является content-superuser:
upload не выдаёт implicit `read_content`, а policy changes подчиняются ADR-011.

## 3. Информационная архитектура

```text
Spine Control Plane
├── Knowledge
│   ├── Documents
│   ├── Uploads
│   └── Document details
├── Chat
│   ├── Sessions
│   └── Sources
├── Runs
│   └── Run details
└── Settings
    ├── Workspace
    └── Environment
```

В app shell всегда видны текущие workspace и environment. Если deployment
обслуживает только одно значение, они могут быть read-only, но остаются частью
route/request context.

### 3.1. Knowledge

Список документов показывает:

- opaque source ID, тип и размер оригинала;
- lifecycle/processing status, Security Domain и policy version;
- время загрузки и последней успешной индексации;
- состояние parsing и Context projection;
- отдельные manifest/coverage completeness, canonical drift,
  `freshness_status = current | stale` и `health_status = healthy | degraded`
  через safe operational metadata;
- отдельное `unpublished`/`unavailable` состояние до первой active publication;
- sanitized error и доступное следующее действие.

Название, filename, source identity, current `SourceRevision`, checksum и другие
content-bearing поля показываются только при `read_content`. Counts, errors и
diagnostics не раскрывают существование или содержание filtered material.

Минимальные состояния документа:

```text
uploaded → parsing → indexing → ready
                     ↘ failed
ready → deleted
```

Загрузка поддерживает drag-and-drop нескольких файлов, предварительную проверку
типа и размера, отдельный прогресс и результат для каждого файла. Retry одной
логической upload command с тем же `Idempotency-Key` возвращает стабильный
результат. Новый upload без explicit target создаёт отдельный `SourceObject` даже
при тех же bytes/filename; replacement явно указывает stable object ID, а
изменившийся canonical revision digest создаёт новую ревизию.

Карточка документа содержит оригинальные метаданные, историю ревизий, stable
locators, извлечённый текст или chunks, состояние проекции, warnings и failures.
Удаление создаёт tombstone. Физическое необратимое удаление не является обычным
действием интерфейса.

### 3.2. Chat

Chat позволяет администратору проверять один встроенный capability
`answer_question`. Выбор произвольного агента, команды или workflow в R1 не
предусмотрен.

Экран поддерживает:

- создание и открытие тестовой сессии;
- выбор доступного knowledge scope, когда в workspace их несколько;
- потоковое отображение состояния выполнения и ответа;
- явный abstention или запрос уточнения при недостатке evidence;
- citations рядом с подтверждаемыми claims после прохождения disclosure gate;
- инспектор source revision, locator и использованного excerpt;
- freshness/as-of и версию Context projection;
- раздельные Target Completeness Scope и Observed Coverage без раскрытия
  unauthorized membership, exclusions или counts;
- переход к связанному run;
- feedback: полезность ответа, неверный ответ и неверная citation.

User-supplied parameters и hypotheses показываются как inputs/assumptions, а не
как source evidence. Session history помогает разрешать references только пока
она current-authorized; prior answer никогда не отображается как evidence нового.
`clarification_required` создаёт новый linked run после ответа пользователя.

Для explicit exhaustive request UI не предлагает partial result по умолчанию.
Non-exhaustive fallback возможен только после explicit request/preference и
показывает, что Target Completeness Scope не покрыт Observed Coverage. Current
request completeness constraints имеют precedence над сохранённой preference.

Chat не показывает скрытую chain-of-thought. Вместо неё доступны структурированная
сводка, использованные источники, версии, решения gate и диагностические события.

### 3.3. Runs

Runs — операционный журнал вопросов и ingestion-проверок, а не полная workflow
Control Plane будущих релизов. Список показывает status, initiator, session,
started at, duration и наличие ошибок.

Карточка Q&A run показывает:

- workspace, environment, acting subject и trace ID;
- закреплённые версии agent, prompt, context profile и projection;
- attempts с отдельно pinned snapshot/boundary/as_of без смешения evidence;
- retrieval strategy и найденные references;
- источники, использованные и отклонённые при формировании ответа;
- итоговый outcome: `answered`, `clarification_required`, `abstained`, `denied`,
  `failed` или `cancelled`;
- независимые признаки completeness/partiality и grounded conflict для answered
  result;
- Target Completeness Scope, Observed Coverage и serving/validation decisions в
  пределах текущей content authorization;
- latency, token/cost metrics, warnings и structured errors.

Начальная реализация может использовать линейную timeline. Tree/waterfall для
всех model и tool calls добавляется только после появления реальной вложенности.

### 3.4. Settings

В R1 Settings преимущественно read-only и показывает:

- текущие workspace и environment;
- health API и Cognee adapter;
- активный `ProjectionSnapshot` и его `as_of` boundary;
- PostgreSQL-controlled activation generation, publication/rollback status и
  pending catch-up/reconciliation state;
- model и embedding provider без credentials;
- последнюю успешную ingestion/projection operation;
- версии parser, extractor и projection configuration, полезные для диагностики.

## 4. Общие состояния и обратная связь

Каждый экран проектируется для состояний:

```text
loading | empty | partial | stale | degraded
permission_denied | validation_failed | failed | ready
```

Ошибка содержит понятное описание, trace ID и безопасное следующее действие, но
не раскрывает credentials, защищённый текст или внутренний stack trace.

Для длительной операции интерфейс показывает переход
`accepted → running → terminal state`; обновление может поступать через SSE с
polling fallback. Повтор команды использует тот же idempotency key, когда это
retry одной логической операции.

## 5. API-границы первого среза

UI использует версионированные endpoints под `/api/v1` и сгенерированные из
OpenAPI типы. Минимальные ресурсы:

- documents и source revisions;
- uploads/ingestion runs;
- projection status;
- Q&A sessions и messages;
- Q&A runs и evidence references;
- feedback;
- health/configuration summary.

API возвращает structured errors и сохраняет workspace, environment, acting
subject и trace ID. Ответ Q&A включает как минимум outcome, structured citations
для answered result, disclosure-safe reason для non-answer outcome, `as_of` и
projection snapshot. Versioned confidence metadata optional и не заменяет gate.

## 6. Не входит в R1

- чат для обычных сотрудников;
- visual agent или workflow builder;
- выбор агентов, teams и произвольных workflows в Chat;
- публикация и rollback agent deployments через UI;
- approvals, schedules и вмешательство в активный run;
- learning memory и ручное редактирование model conclusions;
- универсальный Context Graph editor;
- dashboard бизнес-метрик и общий Insights;
- настройка сложной RBAC/ABAC-модели через UI.

Эти функции добавляются только в том vertical slice, который подтверждает их
необходимость.

## 7. Критерии первого интерфейсного релиза

- Администратор загружает поддерживаемый файл без CLI и видит отдельный результат
  его обработки.
- Новая версия и повторная загрузка не уничтожают историю и не создают
  неконтролируемые дубликаты.
- После успешной индексации администратор задаёт контрольный вопрос через Chat.
- Каждый выданный ответ содержит доступную citation на конкретную ревизию и
  stable locator; иначе Chat показывает abstention.
- Chat не представляет partial result как exhaustive, если snapshot exclusions,
  drift или Context Profile не позволяют подтвердить необходимую полноту.
- UI не считает `healthy` достаточным для ответа: serving decision остаётся
  request-specific результатом authorization, validity, evidence и profile gates.
- Из Chat можно открыть run и проследить вопрос до retrieval evidence и версий
  исполнения.
- Ошибку parsing, projection или generation можно локализовать без доступа к
  серверным логам, а retry не создаёт второй логический эффект.
- Неавторизованный пользователь не может использовать Chat, читать документ или
  получить его текст из error/trace response.
- Основной сценарий проходит через UI без CLI и прямого обращения к Cognee.

## 8. Решения по реализации интерфейса

Статус решений в этом разделе: **приняты для R1**. Prototype gate проверяет их на
вертикальном сценарии до расширения frontend scope.

### 8.1. Halaska задаёт визуальное направление

[Halaska UI](https://ui.halaska.com/) используется как визуальный и UX-референс
для Spine Control Plane. Наиболее релевантны его паттерны для административных
экранов, streaming, evidence/confidence, feedback, action receipts, recovery и
run diagnostics.

Halaska не становится внешней доменной моделью или runtime Spine. Его единый JSX
kit не добавляется в production-код без изменений и не устанавливается широким
agent prompt поверх всего `web/`. Выбранные части:

- переносятся контролируемым diff из закреплённого upstream commit;
- преобразуются в типизированные `.tsx`-модули;
- раскладываются по `design-system` и consuming feature;
- связываются с design tokens Spine;
- получают component и interaction tests;
- сохраняют требуемое MIT license notice.

Предлагаемая структура:

```text
web/
  design-system/
    tokens/
    primitives/
    patterns/
      evidence/
      feedback/
      run-timeline/
      status/
  features/
    knowledge/
    chat/
    runs/
```

Сложные интерактивные элементы — dialog, menu, combobox, select, tooltip —
строятся на Base UI, даже если их внешний вид следует Halaska. В R1 не смешиваются
две primitive libraries.

### 8.2. Chat принадлежит Spine

Поведение Chat реализуется как небольшой Spine-owned feature. Каноническое
состояние sessions, messages, runs, citations и feedback находится на backend;
frontend не создаёт второй источник истины.

```text
Halaska-derived presentation
  → Spine chat state and transport adapter
  → generated OpenAPI client + SSE
  → Spine API
  → answer_question / Context Broker
```

Минимальная frontend-граница:

```ts
interface ChatTransport {
  createSession(): Promise<Session>;
  getSession(sessionId: string): Promise<Session>;
  listMessages(sessionId: string): Promise<Message[]>;
  sendMessage(
    sessionId: string,
    input: SendMessageInput,
    signal: AbortSignal,
  ): AsyncIterable<ChatEvent>;
  cancelRun(runId: string): Promise<void>;
  submitFeedback(input: FeedbackInput): Promise<void>;
}
```

Streaming использует типизированные события Spine, например `run_started`,
`stage_changed`, `answer_delta`, `citation_added`, `abstained`, `completed` и
`failed`. UI library не определяет wire format публичного API.

Frontend обязан самостоятельно покрыть auto-scroll, send/stop, cancellation,
session hydration, reconnect/partial failure, retry, `Enter`/`Shift+Enter`,
защиту от двойной отправки и связь message → run → citations.

Решение пересматривается, если подтверждённый продуктовый сценарий потребует
branching, редактирования истории, сложных attachments, нескольких агентов,
generative tool UI или других thread operations. `ChatTransport` сохраняет seam,
за которым другой runtime можно подключить позднее без изменения API и доменной
модели.

### 8.3. Выбранный frontend stack

Целевая композиция R1:

```text
React + TypeScript strict
├── Vite                               build и dev server
├── React Router Data Mode              routing и route boundaries
├── Base UI                             headless primitives
├── Halaska-derived patterns            визуальный язык Control Plane
├── CSS variables + CSS Modules         tokens и локальные styles
├── TanStack Query                      server state
├── generated OpenAPI client            request/response contracts
├── Spine ChatTransport + SSE           streaming Q&A
└── Vitest + Testing Library + Playwright
```

Package manager — `pnpm`; его версия фиксируется полем `packageManager` и
lock-файлом. Frontend собирается как SPA и публикуется статическими assets.
FastAPI остаётся единственным backend/control-plane server. Next.js, React Server
Components, Server Actions и отдельный Node BFF в R1 не используются.

Halaska и любой будущий chat runtime остаются presentation/adaptation слоем. Они
не владеют `SourceRevision`, `ProjectionSnapshot`, `Session`, `Run`, `Citation`,
`Feedback` или access policy и не обращаются напрямую к Cognee, Temporal, model
provider или базе данных.

### 8.4. Prototype gate

До расширения frontend scope прототип должен доказать один вертикальный сценарий:

1. Загрузить документ и показать progression до `ready` или `failed`.
2. Создать и восстановить Q&A session с backend.
3. Получить streaming answer с двумя structured citations.
4. Открыть citation inspector с `SourceRevision` и stable locator.
5. Показать abstention и structured error.
6. Отменить run и безопасно повторить его как новый run с lineage.
7. Перейти от message к run diagnostics.

После этого версии dependencies и upstream revision Halaska закрепляются в
`package.json`, lock-файле и документации `web/`.

## 9. Технические требования frontend R1

### 9.1. Маршруты и URL state

Минимальные маршруты:

```text
/knowledge/documents
/knowledge/documents/:documentId
/chat/:sessionId
/runs/:runId
/settings
```

URL сохраняет выбранный документ, session/run, pagination, filters, tab,
workspace и environment. Состояние, на которое требуется прямая ссылка или
восстановление после reload, не хранится только в component state.

### 9.2. Авторизация и request context

- Все страницы и API-команды доступны только роли `administrator`.
- Каждый запрос несёт acting subject, `workspace_id`, environment и trace ID.
- Сервер проверяет роль и scope; скрытие control в UI не является авторизацией.
- Protected metadata, document content, Chat output, citations, evidence и
  content-bearing run history дополнительно требуют current `read_content` ко
  всем contributing Source Objects.
- Мутации используют `Idempotency-Key`.
- Credentials и provider keys не попадают в browser bundle, URL, telemetry или
  client logs.

### 9.3. Server и local state

TanStack Query владеет documents, revisions, ingestion runs, sessions, messages,
Q&A runs, feedback и health/configuration summary. Server state не копируется в
отдельный глобальный store. Component state используется для draft, открытых
панелей и других несохранённых UI-состояний.

Frontend API types и client генерируются из OpenAPI. Файлы под
`web/api/generated/` не редактируются вручную; CI проверяет их регенерацией.

### 9.4. Streaming

SSE-протокол поддерживает как минимум:

```text
run_started | stage_changed | answer_delta | citation_added
clarification_required | abstained | denied | completed | failed | cancelled
heartbeat
```

Каждое событие имеет event ID, session/message/run/trace IDs и versioned payload.
Клиент поддерживает упорядочивание, deduplication, reconnect/resume, heartbeat,
timeout, cancellation и terminal state. После terminal event клиент запрашивает
каноническое сообщение у API; transient deltas не становятся единственным
источником сохранённого ответа.

`answer_delta` и `citation_added` не передают candidate model output. Они могут
транслировать только content из уже принятого Grounded Answer после полного
claim/evidence disclosure gate и current authorization check перед protected
event. До этого stream показывает только safe progress. Failure validation или
authorization change завершает stream safe terminal event без undisclosed
answer content.

Public terminal payload использует disclosure-safe reason и одинаково безопасную
структуру для source-sensitive причин. Protected diagnostics и source-specific
details запрашиваются отдельно и только после content authorization. UI и
telemetry не должны создавать очевидно различимые source-dependent detail/count
paths; timing leakage проверяется security tests без обещания constant time.

Server-side event log фиксирует emission/event identity для resume и audit, но не
объявляет emission доказательством client receipt. Accepted answer content всегда
гидратируется из canonical persisted message после terminal event.

### 9.5. Upload и ingestion

- Поддерживаются batch upload, progress и отдельный результат каждого файла.
- Client validation проверяет формат, размер и количество до отправки.
- Upload можно отменить и безопасно повторить.
- `uploading`, `uploaded`, `parsing`, `indexing`, `ready`, `rejected` и `failed`
  являются разными состояниями.
- Retry той же logical command с тем же `Idempotency-Key` возвращает стабильный
  результат; отдельный upload не объединяет `SourceObject` по checksum.
- Replacement явно указывает stable object ID; новый canonical revision digest
  создаёт `SourceRevision`, exact duplicate observation — нет.
- Manual replacement и restoration отправляют expected current revision;
  concurrent stale command получает conflict без нового canonical effect.
- Partial failure одного файла не скрывает успешную обработку остальных.

### 9.6. Citations и AI output

Citation является immutable типизированным объектом с opaque identity,
SourceObject/SourceRevision provenance, original digest, versioned stable locator,
exact ContextBundle evidence reference и immutable run linkage к `as_of` и
projection snapshot, а не только Markdown-ссылкой. Claim-to-evidence linkage
является server-owned contract; display metadata и inline marker не authoritative.

Inspector разрешает citation только на сервере и повторно проверяет current
policy, source lifecycle и retention. Он никогда не применяет historical locator
к replacement revision. Historical excerpt показывается с явной маркировкой
только в разрешённом ADR 0018 history/citation-inspection flow; иначе UI
показывает safe unavailable state без protected distinctions. Stored excerpt и
cache не являются fallback authority.

Model output считается недоверенным: Markdown санитизируется, произвольные HTML,
script, iframe и model-generated JavaScript не исполняются. UI не показывает
system prompts, credentials, внутренние provider errors или chain-of-thought.

### 9.7. Состояния и ошибки

Каждый route и самостоятельная data panel реализует необходимые состояния
`loading`, `empty`, `partial`, `stale`, `degraded`, `permission_denied`,
`validation_failed`, `failed` и `ready`. Отдельно обрабатываются недоступность
API/Cognee, устаревшая projection, разрыв SSE, partial answer, отменённый run и
недоступная citation.

Partiality и grounded conflict отображаются независимо: один answered result
может быть одновременно limited по completeness и показывать несколько
противоречивых grounded positions. Ни degraded status, ни найденные citations
сами по себе не позволяют UI называть ответ complete.

Stored answer content показывается только после current disclosure-eligibility
check для всех Disclosure Dependencies. При revocation, supersession, tombstone
или retention restriction скрывается весь content-bearing history entry; UI не
делает partial redaction и показывает только independently authorized safe status.

Numerical confidence не является обязательным UI field. Если оно показывается,
UI объясняет named/versioned policy и meaning score и не представляет его как
probability correctness, completeness или authorization.

Structured error содержит стабильный code, безопасное описание, retryability и
trace ID. Raw stack trace и защищённые данные в response не возвращаются.

### 9.8. Локализация и форматирование

R1 выпускается на русском языке, но пользовательские строки не разбрасываются по
JSX. Даты, числа, длительности и размеры форматируются через `Intl`. Frontend
переводит стабильные backend codes в product copy; текст backend error не
используется как единственный способ определить тип ошибки.

### 9.9. Производительность

- Routes загружаются лениво.
- Lists используют server-side cursor pagination.
- Streaming updates объединяются так, чтобы не перерисовывать весь thread на
  каждый token.
- Устаревшие requests отменяются.
- Большие таблицы виртуализируются только после подтверждения объёма данных.
- Halaska kit декомпозируется; весь исходный kit не включается в один runtime
  bundle.

### 9.10. Наблюдаемость

Frontend telemetry содержит route/action, stable error code, trace ID, frontend
release, API latency и SSE reconnect count. Полные тексты документов, questions,
answers, citation excerpts, credentials и сырые provider payloads не отправляются
без отдельной retention/privacy policy.

### 9.11. Тестовые требования

- Vitest проверяет state reducers, formatters, transport и другие чистые
  frontend contracts.
- Testing Library проверяет components через пользовательские действия и
  видимые outcomes.
- Playwright проверяет критический путь через реальный browser и fake/controlled
  API boundary.
- API fixtures типизированы и синтетические; сеть и provider availability не
  требуются для default suite.
- Обязательные interface scenarios: batch upload с partial failure, полный
  ingestion lifecycle, streaming answer, abstention, SSE reconnect, citation
  inspector, permission denied без утечки, cancel/retry без duplicate run и
  восстановление session после reload.
- Streaming tests доказывают отсутствие candidate content, fail-closed behavior
  и authorization change между validation и каждым protected SSE emission.
- History tests скрывают previously authorized stored answer после revocation,
  supersession, tombstone или retention restriction любой Disclosure Dependency.
- Exhaustive-answer UI states различают Target Completeness Scope и Observed
  Coverage, включая newly accepted source, отсутствующий в active projection.
- UI tests не заменяют release-relevant end-to-end verification configured
  PostgreSQL, Cognee, Temporal и model/validation stack.
