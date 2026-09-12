# Skills Matt Pocock: справочник для Spine

В репозитории установлены skills из
[`mattpocock/skills`](https://github.com/mattpocock/skills). Они задают повторяемые
процессы для исследования, проектирования, декомпозиции, реализации и проверки
изменений.

Этот файл помогает выбрать подходящий skill. Источником истины для поведения
каждого skill остаётся его `SKILL.md` в `.agents/skills/<name>/`.

## Как вызывать skills

В примерах ниже используется синтаксис Codex:

```text
$skill-name Описание задачи
```

Skill также можно выбрать через интерфейс выбора skills. Некоторые skills могут
подключаться автоматически по смыслу запроса; skills с
`disable-model-invocation: true` нужно вызывать явно.

Пример:

```text
$grill-with-docs Спроектируй R1 — Q&A по внутренним документам из ROADMAP.md.
```

## Рекомендуемый процесс для Spine

Обычная работа над продуктовым кейсом проходит так:

```text
$ask-matt
    ↓
$grill-with-docs
    ↓
$to-spec
    ↓
$to-tickets
    ↓
новая сессия $implement для каждого ticket
    ├── $tdd
    └── $code-review
```

Для текущего R1 рекомендуемая последовательность:

```text
$grill-with-docs Разреши продуктовые и технические вопросы R1 — Q&A по
внутренним документам. Обновляй CONTEXT.md и фиксируй архитектурные решения в ADR.

$to-spec Оформи согласованный R1 как спецификацию в GitHub Issues.

$to-tickets Разбей спецификацию R1 на вертикальные tracer-bullet tickets с
явными зависимостями.

$implement Реализуй GitHub issue #<number>.
```

Перед skills, работающими с GitHub Issues, должен быть настроен `gh`:

```bash
gh auth login -h github.com
```

## Основной flow

| Skill | Когда использовать | Пример для Spine |
| --- | --- | --- |
| `ask-matt` | Неясно, какой skill или процесс выбрать | `$ask-matt Что делать после согласования архитектуры R1?` |
| `setup-matt-pocock-skills` | Один раз при подключении набора или смене issue tracker | `$setup-matt-pocock-skills` |
| `grill-with-docs` | Нужно тщательно разрешить вопросы проекта и сохранить решения в glossary/ADR | `$grill-with-docs Спроектируй ACL и citations для Q&A` |
| `grill-me` | Нужно такое же интервью вне рабочего репозитория, без записи документов | `$grill-me Помоги проверить идею продукта` |
| `to-spec` | Обсуждение завершено; нужно синтезировать спецификацию и опубликовать её в tracker | `$to-spec Оформи обсуждение Context Broker как spec` |
| `to-tickets` | Нужно разбить spec на самодостаточные задачи с blocking edges | `$to-tickets Разбей spec #12 на вертикальные tickets` |
| `implement` | Есть согласованный spec или готовый ticket | `$implement Реализуй issue #18` |
| `tdd` | Нужна конкретная функция или исправление через red-green-refactor | `$tdd Добавь проверку неизвестных зависимостей workflow` |
| `code-review` | Нужно проверить diff относительно commit, branch или merge-base | `$code-review Проверь изменения относительно main` |
| `triage` | Пришла внешняя, ещё не подготовленная bug report или feature request | `$triage Разбери GitHub issue #27` |
| `diagnosing-bugs` | Есть сложная ошибка, regression, flake или деградация производительности | `$diagnosing-bugs /ask иногда возвращает пустой ответ после reindex` |
| `wayfinder` | Проект настолько большой и туманный, что путь не помещается в одну сессию | `$wayfinder Спланируй переход от R1 к multi-tenant production deployment` |

`triage` не нужен для tickets, созданных через `to-tickets`: они уже должны быть
готовы для агента.

## Исследование и проверка решений

| Skill | Когда использовать | Пример для Spine |
| --- | --- | --- |
| `research` | Нужен обзор первичных источников с результатом в Markdown | `$research Изучи актуальные Cognee APIs для provenance и permissions` |
| `prototype` | Решение нельзя уверенно выбрать на бумаге; нужен одноразовый UI или logic prototype | `$prototype Сравни три варианта citation inspector для Q&A` |
| `handoff` | Нужно передать контекст в другую сессию, harness, директорию или человеку | `$handoff Подготовь контекст для отдельного прототипа Q&A UI` |
| `claude-handoff` | Нужна передача текущей работы свежему background-agent в поддерживаемой среде | `$claude-handoff Продолжи исследование retrieval strategies` |
| `to-questionnaire` | Решение зависит от знаний внешнего domain expert | `$to-questionnaire Подготовь вопросы владельцу архива коммерческих предложений` |
| `wizard` | Остались действия, которые обязан выполнить человек: credentials, dashboard или cutover | `$wizard Настрой ключи LLM provider и GitHub secrets` |

`prototype` отвечает на один конкретный вопрос. Его результат нужно вернуть в
основной flow через документацию или handoff, а не превращать незаметно в
production-код.

## Домен и архитектура кода

| Skill | Когда использовать | Пример для Spine |
| --- | --- | --- |
| `domain-modeling` | Термины неоднозначны, нужен `CONTEXT.md` или ADR | `$domain-modeling Разведи понятия SourceObject, SourceRevision и Document` |
| `codebase-design` | Нужно определить глубокий модуль, его interface и seam | `$codebase-design Спроектируй узкий интерфейс Context Broker` |
| `improve-codebase-architecture` | Нужен обзор codebase и отчёт о возможностях углубления модулей | `$improve-codebase-architecture Проверь архитектуру после завершения R1` |
| `resolving-merge-conflicts` | Репозиторий уже находится в merge/rebase conflict | `$resolving-merge-conflicts Разреши текущий rebase по намерениям обеих сторон` |

`domain-modeling` отвечает за язык бизнеса и решения. `codebase-design` отвечает
за форму модулей и качество их интерфейсов.

## Обучение и коммуникация

| Skill | Когда использовать | Пример для Spine |
| --- | --- | --- |
| `teach` | Нужно изучать тему в несколько сессий с сохранением прогресса | `$teach Научи меня проектировать retrieval evaluations` |
| `wait-what` | Последнее объяснение оказалось непонятным | `$wait-what` |
| `grilling` | Нужен сам механизм строгого интервью без дополнительного wrapper | `$grilling Проверь моё решение по модели permissions` |
| `writing-for-agents` | Создаётся skill, `AGENTS.md` или документ, на который ссылаются инструкции агента | `$writing-for-agents Улучши структуру AGENTS.md` |

## Специализированные skills

Эти skills установлены вместе с набором, но сейчас не входят в основной Python
flow Spine.

| Skill | Назначение | Когда может понадобиться |
| --- | --- | --- |
| `setup-pre-commit` | Настраивает Husky, lint-staged, форматирование, typecheck и tests | Когда web-часть станет JavaScript/TypeScript-проектом и будет выбрана Node toolchain |
| `setup-ts-deep-modules` | Добавляет dependency-cruiser и границы глубоких TypeScript-модулей | После появления реальной структуры frontend packages |
| `migrate-to-shoehorn` | Заменяет TypeScript `as` assertions в тестах на `@total-typescript/shoehorn` | Только при наличии соответствующих TypeScript tests |
| `git-guardrails-claude-code` | Добавляет Claude Code hooks против опасных Git-команд | Только для разработчиков, использующих Claude Code |
| `scaffold-exercises` | Создаёт структуру учебных упражнений | Для отдельного образовательного материала, не для продукта Spine |

Пример позднего использования:

```text
$setup-ts-deep-modules Настрой границы frontend-модулей после выбора структуры web.
```

## Экспериментальные и in-progress skills

Следующие skills установлены из `skills/in-progress`. Их контракты и поведение
могут меняться; используйте их осознанно.

| Skill | Назначение | Пример |
| --- | --- | --- |
| `implement-spec` | Реализует готовую спецификацию напрямую | `$implement-spec Реализуй спецификацию docs/specs/q-and-a.md` |
| `loop-me` | Помогает интервьюировать пользователя о проектируемых workflows | `$loop-me Спроектируй workflow генерации КП` |
| `retro` | Проводит ретроспективу завершённой coding session | `$retro Разбери реализацию первого Q&A ticket` |
| `writing-fragments` | Извлекает необработанные фрагменты будущего текста | `$writing-fragments Собери материал для статьи об архитектуре Spine` |
| `writing-shape` | Превращает материал в структуру статьи по абзацам | `$writing-shape Сформируй статью из собранных фрагментов` |
| `writing-beats` | Собирает материал в последовательность смысловых beats | `$writing-beats Построй повествование о переходе от Q&A к workflows` |

Для обычной реализации Spine используйте стабильный путь `to-spec → to-tickets
→ implement`, пока экспериментальный skill не решает явно обозначенную проблему
лучше.

## Как выбрать skill по ситуации

| Ситуация | Начать с |
| --- | --- |
| Есть идея, но много нерешённых вопросов | `grill-with-docs` |
| Обсуждение уже завершено | `to-spec` |
| Есть большая спецификация | `to-tickets` |
| Есть готовый ticket | `implement` |
| Есть конкретное поведение, которое удобно задать тестом | `tdd` |
| Что-то сломано и причина неочевидна | `diagnosing-bugs` |
| Нужны свежие сведения из официальных источников | `research` |
| Нужен UI/logic experiment | `prototype` |
| Термины или границы предметной области расплывчаты | `domain-modeling` |
| Нужен обзор качества модульной архитектуры | `improve-codebase-architecture` |
| Пришла неподготовленная внешняя заявка | `triage` |
| Масштаб работы больше одной связной сессии | `wayfinder` |
| Неясно, что выбрать | `ask-matt` |

## Обновление skills

Установленные файлы принадлежат репозиторию, а их версии и hashes записаны в
`skills-lock.json`. Изменения upstream не применяются автоматически.

Обновить конкретный skill:

```bash
npx skills@latest update <skill-name>
```

После обновления проверьте diff: `SKILL.md` содержит исполняемые агентом
инструкции и влияет на его поведение. Локальные настройки Spine находятся в
`AGENTS.md` и `docs/agents/` и имеют отдельный жизненный цикл.
