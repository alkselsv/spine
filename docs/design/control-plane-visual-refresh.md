# Визуальное обновление Control Plane

Статус: реализовано как Halaska-based presentation runtime поверх штатных
маршрутов, scope, авторизации и disclosure-контрактов Spine.

## Референсы

- [Agno Control Plane](https://www.agno.com/products/control-plane) задаёт
  композицию: постоянная боковая навигация на всю высоту с иконками и
  группами, рабочая область в отдельной рамке, верхняя контекстная панель с
  индикатором соединения и действием обновления, плотные таблицы с
  моноширинными uppercase-заголовками и статус-чипы.
- [Halaska UI](https://ui.halaska.com/) (pinned revision в
  [`web/design-system/NOTICE.md`](../../web/design-system/NOTICE.md)) задаёт
  визуальный язык: нейтральная палитра, тонкие границы без тяжёлых теней,
  моноширинный eyebrow над заголовком, тёмные pill-кнопки, паттерн `status`
  и контейнер `prompt-input`, переключатель светлой/тёмной темы.

Закреплённые `web/src/ported/halaska-kit.jsx` и
`web/src/ported/spine/SpineApp.jsx` подключены непосредственно из production
`appRoutes`. Они образуют единственный presentation graph для темы, shell и основных
экранов; React Router, auth boundary, generated API clients и URL scope остаются
Spine-owned адаптациями внутри этого graph. Qualification и fixture builds не имеют
альтернативного shell. Простое оборачивание другого интерфейса в Halaska
`ThemeProvider` не выполняет это требование.

## Сопоставление

| Область Spine | Элемент Agno | Паттерн Halaska или fallback | Адаптация Spine | Отклонение |
| --- | --- | --- | --- | --- |
| Shell | Sidebar с org switcher, группами разделов и пользователем внизу | Sidebar density, mono section labels | `SpineApp` + `SpineSidebar`: бренд, группы «Работа» и «Наблюдение», «Настройки» внизу, роль и переключатель темы в футере | Нет org switcher: Workspace и Environment остаются server-managed scope в URL |
| Контекстная панель | Селектор OS со статус-точкой и `Refresh` | `status-dot` | Workspace / Environment как компактные Base UI Select, статус соединения и «Проверить соединение» справа | Ровно один `role="status"` в shell сохранён для существующих контрактов |
| Заголовки страниц | Breadcrumb и плотный заголовок | Mono eyebrow, title 1.45rem | Общие `.eyebrow`/`h2` во всех feature-модулях | — |
| Списки и таблицы | Таблица с uppercase-заголовками, hover и выделением строки | `table`, `data-table` | Mono-заголовки, sunken header, hover строки, ссылки строк подчёркнуты | Сортировка и чекбоксы не добавлены: порядок и пагинация остаются серверными |
| Статусы | Чипы `COMPLETED` / `PROCESSING` | `badge`, `status` | `.statusPill`, `.statusChip`, `.uploadState` — mono uppercase с рамкой; `StatusPanel` — строка со статус-точкой по тону состояния | Тон задаётся состоянием интерфейса, текст состояния не меняется |
| Chat composer | Поле ввода внизу потока | `prompt-input` | Один мягкий контейнер, поле без рамки, кнопка отправки справа, focus ring на контейнере | Вложения, выбор агента и модели не добавлены: их нет в контракте Q&A |
| Тема | Тёмный интерфейс | Light/dark toggle | Светлая тема по умолчанию следует системе; выбор сохраняется в `localStorage` (`spine.theme`) и применяется до рендера | Тема — локальная настройка браузера, не серверная |

## Проверка

- `web/src/app.test.tsx` проверяет характерную DOM-структуру исходного `SpineApp`,
  а не только runtime marker, и покрывает navigation, scope и тему.
- Playwright fixture qualification открывает Knowledge, Chat и Q&A Runs по
  чистым URL и проверяет тот же runtime marker вместе с данными API fixtures.
- Существующие unit- и Playwright-сценарии, включая отсутствие
  горизонтального переполнения на 390px, проходят без изменений.
