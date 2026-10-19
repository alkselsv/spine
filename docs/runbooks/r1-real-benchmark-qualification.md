# Локальная real qualification R1

Этот runbook описывает явный операторский запуск утверждённого real R1 benchmark
через integrated runner #480, observer #478 и общую production Q&A composition
#469. Команда не запускается автоматически в CI. CI выполняет только offline
контракты с synthetic/fake зависимостями; их результат не является real
qualification.

## Границы и разрешение

Перед использованием частного корпуса получите отдельное разрешение владельца
данных на выбранные dataset и source revisions. Issue #453 и ADR-0039/0040 такого
разрешения не дают. Не меняйте identity, membership, grants, AccessPolicy,
benchmark mapping или источники ради успешного прогона. Секреты, mapping,
dataset и исходные документы остаются в разрешённых локальных хранилищах и не
попадают в Git, CI artifacts, Issues, PR, логи или отчёт.

Только локальный composition factory связывает deployment configuration с
runtime. Передайте через `--composition` factory, которая возвращает точный тип
`RealBenchmarkQualificationRuntime`, содержащий `RealBenchmarkRunner` и
content-free `TrustedRealBenchmarkPreflight`, созданный с evidence того же
production composition, что использует runner. Не передавайте обычный
`RealBenchmarkPreflightPort`: его ответы подходят для deterministic tests, но
не разрешают real run. Runner должен использовать
`RealBenchmarkProductionObserver`, обёрнутый вокруг
`DurableProductionQAAFlow`. Fake observer, capability, authorization или
preflight могут использоваться в детерминированных тестах, но не проходят
production qualification gate. The gate verifies the concrete durable
composition, persisted Q&A capability and sessions, HTTP capture channel,
authorized Context Broker, observer admission binding, registered Temporal
workflow/activity wiring, shared SDK client and terminal waiter wiring. Each
observer context is verified by the same trusted request boundary used by
PostgreSQL before admission. A caller-supplied mode flag cannot promote a fake
composition to a production qualification.

## Предварительные проверки и владельцы

Preflight возвращает только состояние и bounded reason code для каждого пункта.
Не печатайте значения credentials, пользовательский текст, пути, имена файлов
или opaque case/source identifiers.

| Prerequisite | Владелец | Безопасная проверка |
| --- | --- | --- |
| PostgreSQL | Runtime/database owner | Локальная health/read-only connectivity проверка настроенного canonical DB и нужных application roles; не выводить DSN. |
| Temporal и worker | Runtime/workflow owner | Проверить доступность настроенного namespace, task queue и worker через health-интерфейс; admission ACK не считается terminal completion. |
| Trusted authentication | Identity owner | Убедиться, что настроенный principal разрешается существующим canonical directory; не создавать benchmark identity и не печатать token. |
| Authorization scope | Security owner | Проверить актуальные membership, environment, role, route grant, generation и AccessPolicy через trusted request context; CLI selectors не являются authority. |
| Production composition | Q&A runtime owner | Использовать общий durable API/Temporal composition, terminal waiter и authorized persisted readback. Отдельный retrieval или inline capability вызов не подходит. |
| Capture channel | Runtime/security owner | Проверить authenticated channel и его доступность между observer и worker. Capture должна совпасть с canonical execution identity и быть same-execution. |
| Canonical corpus permission | Data owner | Получить отдельное разрешение и локально проверить его запись/идентификатор без копирования в отчёт. |
| Canonical corpus | Ingestion/data owner | Через readiness contract подтвердить accepted/current revisions и завершённый canonical lifecycle. Ingestion receipt недостаточен. |
| Active projection | Retrieval owner | Через readiness contract подтвердить точное membership требуемых revisions в active healthy/current publication для trusted scope. Не писать напрямую в индекс. |
| Approved dataset | Evaluation/data owner | Явно выбрать approved pinned real dataset/version; loader должен подтвердить digest и структуру. Synthetic fallback запрещён. |
| Local inputs | Evaluation operator | Проверить разрешённые локальные dataset, source root и versioned mapping manifest; не добавлять их в репозиторий. |

Если preflight недоступен, возвращайте `preflight_unavailable`. Если точный
prerequisite отсутствует — `missing:<name>`; если проверка доступа запрещена —
`denied:<name>`; если сервис нельзя проверить — `unavailable:<name>`. Любой
не-ready пункт останавливает run до mapping, readiness observation и Q&A.
Отсутствующий/неизвестный composition не может быть заменён fake.

## Запуск

Проверьте локальную команду и пути заранее, затем оператор явно запускает её из
корня репозитория с разрешёнными локальными входами:

```bash
uv run spine-eval-real-benchmark \
  --dataset-kind real \
  --dataset-dir /approved/local/r1-dataset \
  --source-root /approved/local/source-root \
  --mapping-manifest /approved/local/r1-runtime-mapping.json \
  --workspace-id "$R1_WORKSPACE_SELECTOR" \
  --environment-id "$R1_ENVIRONMENT_SELECTOR" \
  --composition local_r1_runtime:build_qualification_runtime \
  --output /approved/local/reports/r1-qualification.json
```

Замените пути, selectors и factory на runtime-owned локальные значения. Scope
selectors только выбирают область запроса: авторитетными остаются значения из
проверенного trusted context. Output содержит metadata-only qualification и
benchmark reports. Храните его в утверждённом локальном месте; не прикладывайте
его к PR или issue без проверки владельцем данных.

При блокировке команда завершается с кодом 2 и печатает в stderr только
versioned qualification status и bounded reason codes. `production_observation`
остаётся `not_run`; runner не вызывается. Реальный run допускается только после
повторной проверки всех prerequisites. Команда не переключается на synthetic
dataset при ошибке real selection или validation.

## Статусы и восстановление

Qualification record связывает trusted `workspace_id`/`environment_id` и
разделяет structural validation, production observation,
evaluation, real qualification и release qualification. Успешный production
observation может квалифицировать только факт прохождения завершённого real
benchmark через production composition. Evaluation остаётся отдельным
результатом; release qualification всегда `not_run`. Этот отчёт не применяет
thresholds #17 и не заявляет release readiness.

| Результат | Значение | Действие |
| --- | --- | --- |
| `denied` | Trusted principal или scope не авторизован | Остановиться; обратиться к security owner. Не расширять grants и не менять policy. |
| `unavailable` | Runtime prerequisite не готов, timeout или безопасная terminal outcome отсутствует | Исправить проблему у владельца компонента, заново проверить весь preflight и повторить явный запуск. |
| `partial` | Часть случаев не получила валидное terminal observation | Сохранить только безопасный aggregate report; выяснить bounded failure categories и повторно проверить readiness. Не трактовать как полный benchmark. |
| `failed` | Structural validation, observation или evaluation завершилась ошибкой | Исправить вход/сервис у соответствующего владельца и повторить полную проверку. Не перепривязывать source автоматически и не повторять retrieval отдельно. |
| `qualified` | Все реальные случаи прошли production observation по выбранному pinned dataset | Это qualification результата real benchmark, но не release qualification. Evaluation status читается отдельно. |

Частичный/прерванный запуск не меняет уже принятые canonical records. Перед
повтором проверьте текущие source revisions, активную projection publication,
trusted authorization generation и terminal/capture state. Не обходите
terminal waiter, не используйте workflow result вместо authorized persisted
readback и не заменяйте потерянную capture повторным retrieval.

## Детерминированное evidence

Offline qualification проверяет каждый из 11 prerequisite codes, блокировку до
runner при отсутствии prerequisites, deny до observation, отказ synthetic
selection и fake composition, безопасный сериализуемый отчет и отсутствие
release claim. Эти проверки не доказывают работу PostgreSQL, Temporal,
authentication provider, частного корпуса или production deployment.

Structural validation подтверждает входные schema/digest и mapping contract.
Production observation выполняет gold-blind request через авторизованный
production Q&A. Evaluator присоединяет gold только после проверки и финализации
observation. Release qualification — отдельное governed решение и в этом
benchmark не выполняется.
