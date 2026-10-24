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

### SQL в report.py / funnel.py / sandbox.py
- CTE-based, не subqueries
- Время и дни — готовые колонки `ts_utc` / `day` / `receive_day` из `AppStore.build()` (эффективное время D2); сырые `*_timestamp`/`*_datetime` — VARCHAR, для них арифметики нет
- Значения в запросы — только через плейсхолдеры `?`
- Совпадение имён/параметров — только строгое равенство (`=` / `json_extract_string`), никакого ILIKE и подстрок (аудит 2026-09-28 зафиксировал нечёткие совпадения как дефект)
- `NULLIF(x, 0)` для деления — никогда не допускать division by zero

### Тесты
- `respx` для мока HTTP (не unittest.mock)
- Синтетические данные — `tests/helpers.py`: `make_store()` + `build()` собирают Parquet-партиции и `analytics.duckdb` в tmp_path
- Каждый тест — один assert-сценарий, имя теста описывает что проверяется

### Error handling
- `AppMetricaError(status_code, message, endpoint)` — единственный тип ошибок API
- Tools ловят `AppMetricaError` и возвращают `{"error": ..., "status_code": ...}`
- Не используем ToolError / исключения на уровне MCP — LLM лучше работает с error-dict

## Metric names

Reporting API использует префиксы (`ym:ge:`, `ym:s:`, `ym:i:`, `ym:ce:`, `ym:cr:`); смешивать префиксы в одном запросе нельзя.

Каталог `metrics.py` содержит только имена, подтверждённые живым API (проверка E-038/E-040 от 2026-09-28); невалидные имена туда не возвращать. Новая метрика попадает в каталог только после успешного запроса к реальному API.

## Хранилище сырых данных

- Партиции `<home>/apps/<id>/<table>/day=YYYY-MM-DD.parquet` — receive-день в таймзоне приложения; всё как вернул API (строки), IP-колонки не запрашиваются
- Партиция заменяется целиком (whole-partition replace) — повторный `sync` безопасен, дубликатов не создаёт
- «Закрытый» день = выгружен ≥24 ч после конца дня; sync довыгружает missing/open дни и последние 7 дней
- Перед аналитикой — `sync` затем `build` (пересборка `analytics.duckdb` целиком, атомарная замена); читатели открывают базу read-only

## Что НЕ делать

- Не добавлять write-tools (Post API, Push API)
- Не хардкодить app_id — всегда параметр
- Не кэшировать Reporting API ответы — они уже быстрые
- Не парсить event_json в Python — использовать `json_extract_string()` в DuckDB
- Не добавлять зависимости без явной необходимости (pandas, numpy — не нужны)
- Не логировать OAuth-токен или user data в stdout (MCP = stdio transport)
