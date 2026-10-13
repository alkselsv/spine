# Issue #165 — administrator Control Plane shell

Статус: реализовано в рамках route shell; Knowledge, Chat и Runs остаются
маршрутными границами следующих вертикальных срезов.

## Принятые решения

- Контекст `workspace` и `environment` хранится в query-параметрах URL. При
  прямой ссылке или reload shell восстанавливает только известные значения, а
  неизвестные безопасно заменяет default scope.
- Scope selectors построены на Base UI Select. `scopeQueryKey(resource, scope)`
  включает оба идентификатора, поэтому смена области не может переиспользовать
  cache ключ предыдущей области.
- Корневой route проходит через `AdministratorBoundary`. Состояния
  unauthenticated, expired, unavailable и forbidden имеют разные безопасные
  русские outcomes; наличие навигационной ссылки не считается авторизацией.
- Общий `StatusPanel` задаёт единый контракт для loading, empty, partial, stale,
  degraded, permission-denied, validation-failed, failed и ready. Structured
  API errors проходят через `localizeApiError`; raw backend text не становится
  пользовательским сообщением.
- Форматирование дат, чисел, длительностей и размеров вынесено в русские
  `Intl`-formatters. Продуктовые строки находятся в типизированном resource
  object, а не в route-компонентах.

## Референсы и адаптация

### Agno Control Plane

Источник: [Agno Control Plane](https://www.agno.com/products/control-plane),
проверено 2026-10-13. Принят паттерн административной композиции Knowledge / Chat
/ Runs и отдельной shell-навигации. Отклонены Agno runtime, browser-to-agent
архитектура и доменная модель: Spine UI обращается только к generated API client
и scoped request boundary.

### Halaska UI

Источник: [Halaska UI](https://ui.halaska.com/), upstream repository
[Halaska-Studio/ui](https://github.com/Halaska-Studio/ui/tree/f11ab6958de3fb186a4d431e8425bbe43a6dc87b),
pinned revision `f11ab6958de3fb186a4d431e8425bbe43a6dc87b` (проверено
2026-10-13). Приняты и адаптированы только presentation-паттерны: плотная
административная навигация, компактные status/recovery panels и спокойная
иерархия surface / border / accent. Они переписаны в Spine-owned TSX/CSS Modules,
используют Spine tokens и Base UI primitives.

Отклонены копирование исходного kit, его runtime architecture, transport,
agent/workflow terminology и вторая primitive library. В этой ветке исходный
Halaska source code не копируется; notice с pinned source и лицензией сохранён в
[`web/design-system/NOTICE.md`](../../web/design-system/NOTICE.md) для
отслеживаемости адаптированных presentation patterns.
