# Engineering Rules

## Принципы

1. **Read-only всегда.** Никаких write-операций в AppMetrica. Нет Post API, Push API, campaign modification, event import. Если сомневаешься — не делай.

2. **Данные AppMetrica — источник истины.** MCP не трансформирует, не обогащает, не интерпретирует данные. Считает агрегаты (воронки, retention) и возвращает как есть. Интерпретация — на стороне LLM.

3. **Fail loud, not silent.** Ошибка API → вернуть user-friendly сообщение с кодом и подсказкой. Не глотать ошибки, не возвращать пустые данные без объяснения.

4. **Schema follows real data.** DuckDB-схема определяется реальными CSV-экспортами из AppMetrica, не документацией. Документация может расходиться с реальностью.

## Конвенции кода

### Python
- `from __future__ import annotations` в каждом модуле
- Type hints везде, `Annotated` для tool-параметров
- Dataclass/frozen dataclass для конфигурации и preset-ов
- Pydantic только для моделей API-ответов
- async def для всего что делает I/O, обычные функции для вычислений

### Именование
- Tools: глагол + существительное — `get_traffic`, `build_funnel_report`, `sync_events`
- Preset keys: короткие, без глагола — `traffic`, `retention`, `crashes`
- Внутренние хелперы: `_prefix` — `_run_preset`, `_default_dates`, `_ensure_events_cached`

### SQL в analytics.py
- CTE-based, не subqueries
- Всегда `::TIMESTAMP` cast для datetime VARCHAR перед арифметикой с INTERVAL
- `UBIGINT` для appmetrica_device_id
- `ILIKE` для нечеткого поиска по tracker_name/source
- `NULLIF(x, 0)` для деления — никогда не допускать division by zero

### Тесты
- `respx` для мока HTTP (не unittest.mock)
- `in_memory=True` DuckDB для тестов кэша и аналитики
- Фикстуры в conftest.py через хелперы `_make_event()` / `_make_install()`
- Каждый тест — один assert-сценарий, имя теста описывает что проверяется

### Error handling
- `AppMetricaError(status_code, message, endpoint)` — единственный тип ошибок API
- Tools ловят `AppMetricaError` и возвращают `{"error": ..., "status_code": ...}`
- Не используем ToolError / исключения на уровне MCP — LLM лучше работает с error-dict

## Metric names

Reporting API использует `ym:ge:` prefix. Часть имен верифицирована, часть — нет.

Правило: при добавлении новой метрики в preset — пометить в `metrics.py` как VERIFIED или UNVERIFIED. Верификация = успешный запрос к реальному API.

## Кэширование

- Один DuckDB файл на app_id: `~/.appmetrica_cache/app_{id}.duckdb`
- Append-only — не удаляем данные, не делаем upsert
- Перед аналитическим запросом — проверка `get_cached_date_range()`, дозагрузка gap-ов
- `sync_events` tool — явная синхронизация для batch-запросов

## Что НЕ делать

- Не добавлять write-tools (Post API, Push API)
- Не хардкодить app_id — всегда параметр
- Не кэшировать Reporting API ответы — они уже быстрые
- Не парсить event_json в Python — использовать `json_extract_string()` в DuckDB
- Не добавлять зависимости без явной необходимости (pandas, numpy — не нужны)
- Не логировать OAuth-токен или user data в stdout (MCP = stdio transport)
