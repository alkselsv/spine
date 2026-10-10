# Identity and request-authorization contracts

Модуль `spine.auth` задаёт framework-independent seam для аутентификации и
авторизации защищённых R1-команд и запросов. Он не подключает OIDC, FastAPI или
PostgreSQL и не меняет legacy endpoints.

## Публичные контракты

- `AuthenticationAlias` содержит только точные provider `issuer` и `subject`.
  Alias не является Canonical Human Identity и не предоставляет роль или scope.
- `AuthenticatedAlias` добавляет только версию серверной конфигурации,
  подтвердившей credential. Произвольные token claims через seam не проходят.
- `RequestedAuthorizationScope` — не authority, а явный selector пары Workspace
  и Environment, которую directory обязана проверить.
- `RouteAuthorizationPolicy` — статическая серверная policy с единственной R1
  ролью `administrator`, обязательным Workspace/Environment scope, purpose,
  operation и опциональным server-selected service principal.
- `AuthorizationResolution` — один текущий immutable snapshot разрешённой
  Canonical Human Identity, tenant scope, роли и authorization generation.
- `AuthorizedRequestContext[DiagnosticContextT]` резервирует один типизированный
  slot для канонического Diagnostic Context из отдельного diagnostics seam.
  Это sealed read-only shape без публичной реализации: caller не может создать
  или подменить экземпляр. Trusted implementation, provenance, issuance,
  verification, lifecycle и mapping в `TrustedPersistenceContext` принадлежат
  composition ticket #72.

`AuthenticationPort.authenticate` и
`AuthorizationDirectory.resolve_request_authority` — purpose-specific ports.
Они намеренно не предоставляют CRUD или перечисление identity/membership
записей.

## Реестр и in-memory adapter

`RoutePolicyRegistry` хранит detached validated policy для каждого защищённого
route ID. Дубликат, отсутствующая policy, неизвестный route ID или policy,
созданная в обход validation, останавливают регистрацию.

`InMemoryAuthorizationDirectory` предназначен для application- и contract-тестов.
Он проверяет active alias и Canonical Human Identity, Workspace membership,
Environment ownership и membership, Environment-local role binding и текущую
authorization generation. Unknown, disabled, missing, stale и tenant-mismatch
состояния возвращают одну disclosure-safe категорию `AuthorizationDeniedError`.
Недоступность directory остаётся отдельной
`AuthorizationUnavailableError`.

Auth failures несут стабильные machine-readable `category` и `retryability`,
которые последующий Structured Error mapper переносит без разбора текста
исключения.

PostgreSQL records, trusted context issuance, HTTP mapping и document Access
Policy входят в следующие тикеты спецификации #5 и не являются частью этого
модуля.

## Production OIDC adapter

`spine.infrastructure.auth.OIDCAuthenticationRuntime` реализует production
resource-server adapter поверх `AuthenticationPort`. Runtime владеет одним
лениво используемым async HTTP client, discovery/JWKS cache и refresh lock.
Composition root обязан вызвать `startup()` согласно `readiness_policy`, передать
приложению только `runtime.authentication` и закрыть runtime через `aclose()` или
async context manager.

`OIDCAuthenticationSettings` неизменяемы и версионированы. Каждая запись
`InteractiveClientQualification` является operator assertion для конкретного
client ID, exact issuer, audience и configuration version: это user-facing
client и client-credentials grant для Spine audience на стороне provider
отключён. R1 принимает только явно квалифицированный `RS256`; расширение
algorithm allowlist требует отдельной signed-token/JWK matrix. Readiness
публикует только status, безопасный code, configuration version, issuer origin и
freshness cache; client IDs, evidence references, keys и provider payload в него
не попадают.

Offline conformance harness расположен в `tests/infrastructure/auth/`: injected
transport и clock проверяют discovery, JWKS, подписанные tokens, rotation,
concurrent refresh, stale-key outage и leak corpus без внешнего provider.

Перед production-включением остаётся обязательной отдельная live qualification
точной deployment-конфигурации. Оператор должен подтвердить:

- exact issuer metadata и same-origin HTTPS `jwks_uri` без redirects;
- выпуск RFC 9068 `at+jwt` для exact Spine audience каждым разрешённым client;
- невозможность получить client-credentials token для этой audience каждым из
  этих clients;
- реальную ротацию signing key, expiry/skew behavior и сетевые timeout/failure
  режимы.

Результат live qualification должен ссылаться на ту же
`configuration_version`, что загружена в runtime. Ни один реальный issuer или
client registration в рамках offline-реализации тикета #71 не проверялся.
