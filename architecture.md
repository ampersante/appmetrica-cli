# Architecture

## Обзор

MCP-сервер на Python (FastMCP) поверх AppMetrica API. Три уровня: тонкий HTTP-клиент → локальный DuckDB-кэш → аналитический движок.

```
┌─────────────────────────────────────────────────────┐
│  Claude Code / Desktop / Cursor                     │
│  (MCP client)                                       │
└────────────────────────┬────────────────────────────┘
                         │ MCP protocol (stdio)
┌────────────────────────▼────────────────────────────┐
│  server.py — FastMCP                                │
│                                                     │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐             │
│  │Discovery│ │ Presets  │ │ Advanced │             │
│  │ 2 tools │ │ 10 tools │ │ 4 tools  │             │
│  └────┬────┘ └────┬─────┘ └────┬─────┘             │
│       │           │            │                    │
│  ┌────▼───────────▼────┐ ┌────▼──────────────┐     │
│  │     client.py       │ │   analytics.py    │     │
│  │ (AppMetricaClient)  │ │ (SQL-вычисления)  │     │
│  └──────────┬──────────┘ └────────┬──────────┘     │
│             │                     │                 │
│  ┌──────────▼──────────┐ ┌───────▼───────────┐     │
│  │  AppMetrica APIs    │ │    cache.py       │     │
│  │  • Management (.com)│ │   (DuckDB)        │     │
│  │  • Reporting (.ru)  │ │   ~/.appmetrica_  │     │
│  │  • Logs (.ru)       │ │     cache/        │     │
│  └─────────────────────┘ └───────────────────┘     │
└─────────────────────────────────────────────────────┘
```

## Модули

### server.py (533 строк)
Точка входа. FastMCP-инстанс с lifespan, 16 tools, 2 prompts. Lifespan создает httpx.AsyncClient, AppMetricaClient и EventCache — разделяемые между всеми вызовами tools.

Ответственности:
- Определение всех MCP tools с Annotated-параметрами
- Маршрутизация запросов: preset → client, advanced → cache + analytics
- Автоматическая синхронизация кэша перед аналитическими запросами
- Обработка ошибок AppMetricaError → user-friendly dict

### client.py (140 строк)
Async HTTP-клиент. Три группы эндпойнтов:

- **Management API** (`api.appmetrica.yandex.com`) — `list_applications()`
- **Reporting API** (`api.appmetrica.yandex.ru`) — `get_report()`, `get_drilldown()`
- **Logs API** (`api.appmetrica.yandex.ru`) — `export_logs()` с polling (202 → sleep → retry → 200)

Auth: `Authorization: OAuth {token}` header на httpx.AsyncClient.

Rate limits: 30 req/sec, 5000 req/day. Сервер не имеет локального rate limiter — ошибка 429 транслируется пользователю.

### cache.py (248 строк)
DuckDB-кэш для сырых данных из Logs API. Один файл `.duckdb` на app_id.

Схема откалибрована по реальным CSV-экспортам AppMetrica (33 поля events, 43 поля installations). `appmetrica_device_id` — UBIGINT (значения превышают INT64).

Методы: `store_events()`, `store_installations()`, `get_cached_date_range()`, `query()`.

Стратегия кэширования: append-only, проверка по date range. Если запрошенный диапазон выходит за кэш — дозагружается из Logs API. MVP-ограничение: при повторной синхронизации того же диапазона возможны дубликаты.

### analytics.py (297 строк)
Чистые функции, принимающие DuckDB-соединение, возвращающие dict/list. Никаких API-вызовов.

**build_funnel()** — CTE-цепочка: для каждого шага JOIN с предыдущим + time window. Varchar → TIMESTAMP cast для арифметики с INTERVAL. Source-фильтр через JOIN с installations.

**compute_retention()** — cohort CTE (из installations) + returns CTE (events по days_since_install). LEFT JOIN для сохранения когорт без возвратов.

**compare_cohorts()** — для каждой метрики строит два подзапроса (cohort A и B), вычисляет значение и разницу. Поддерживаемые метрики: `retention_d{N}`, `avg_sessions`, `total_events`, `user_count`.

### metrics.py (133 строки)
Каталог метрик (`ym:ge:` prefix) разделен на verified/unverified. ReportPreset — датакласс с metrics + dimensions + sort. 8 preset-ов: traffic, retention, revenue, crashes, geo, events_top, devices, versions.

GROUP_BY_DIMENSIONS — маппинг коротких имен (date, country, source) на полные dimension names.

### config.py (29 строк)
Frozen dataclass. `from_env()` читает `APPMETRICA_OAUTH_TOKEN` (обязательный) и `APPMETRICA_CACHE_DIR` (опциональный, default `~/.appmetrica_cache/`).

### models.py (59 строк)
Pydantic-модели для типизации: Application, ReportResponse, FunnelResult, RetentionRow, CohortComparisonRow. Используются в документации и тестах; tools возвращают dict для гибкости.

## Потоки данных

### Preset-отчет (get_traffic, get_retention, ...)
```
tool → _run_preset() → client.get_report() → HTTP GET /stat/v1/data.json → dict
```
Один HTTP-запрос, ответ сразу.

### Воронка (build_funnel_report)
```
tool → _ensure_events_cached() → client.export_logs() → poll 202→200 → cache.store_events()
     → analytics.build_funnel() → SQL CTEs на DuckDB → dict
```
Первый вызов медленный (Logs API polling). Повторные — мгновенные из кэша.

### Deep retention
```
tool → _ensure_events_cached() + _ensure_installations_cached()
     → analytics.compute_retention() → SQL CTE cohort + returns → list[dict]
     → optional ratio calculation → dict
```

## Решения и trade-offs

| Решение | Почему | Альтернатива |
|---------|--------|-------------|
| DuckDB, не SQLite | Аналитические запросы (GROUP BY, window functions, JSON extract) на порядок быстрее | SQLite + ручной JSON-парсинг |
| UBIGINT для device_id | Реальные appmetrica_device_id > 2^63 | VARCHAR (медленнее JOIN) |
| Append-only кэш | Простота MVP, Logs API возвращает immutable historical data | Upsert по primary key (сложнее) |
| VARCHAR для datetime | AppMetrica отдает строки, DuckDB умеет cast на лету | TIMESTAMP (ломает загрузку при bad data) |
| Polling в client, не в tool | Инкапсуляция — tool не знает про 202/200 | Background job + callback |
| Preset tools, не enum | LLM лучше выбирает из списка tools, чем из enum-параметра | Один tool с report_type param |

## Безопасность

- Read-only: никаких write-операций, Post API, Push API
- OAuth-токен только в env, никогда в коде или логах
- Все SQL-параметры экранируются через string escaping (не prepared statements — DuckDB limitation для DDL-like queries). Приемлемо, т.к. единственный источник данных — AppMetrica API, не пользовательский ввод.
