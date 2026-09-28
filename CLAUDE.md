# CLAUDE.md

## Проект

AppMetrica MCP — read-only Python MCP-сервер для game analytics поверх AppMetrica API. Позволяет задавать аналитические вопросы на естественном языке из Claude Code/Desktop/Cursor.

## Быстрый старт разработки

```bash
uv sync --extra dev      # установить зависимости
uv run pytest -v         # запустить тесты (30 штук)
uv run python -m appmetrica_mcp  # запустить MCP-сервер (нужен APPMETRICA_OAUTH_TOKEN)
```

## Маршрутизация файлов

Рабочие записи лежат в приватном подмодуле `.private/` (ветка `notes` приватного репо). Без доступа к нему — пропустить.

### При начале сессии — читать в этом порядке:
1. **CLAUDE.md** (этот файл) — правила, структура, конвенции
2. **.private/session-handoff.md** — где остановились, что дальше
3. **.private/tasks.md** — текущий бэклог
4. **.private/context.md** — контекст владельца

### При необходимости (по запросу или контексту):
- **architecture.md** — как устроен сервер, data flow, trade-offs
- **engineering-rules.md** — конвенции кода, что делать / не делать
- **.private/journal.md** — история решений и почему они приняты

### При завершении сессии — обновить в этом порядке:
1. **.private/session-handoff.md** — что сделано, текущее состояние, что дальше
2. **.private/tasks.md** — отметить выполненные, добавить новые
3. **.private/journal.md** — записать ключевые решения сессии с причинами
4. **Коммит записей** — внутри `.private` на ветке `notes`: `git -C .private checkout notes` (после свежего клона там detached HEAD), затем commit и push в `.private`. Указатель подмодуля в основном репо не обновлять.
5. **Коммит кода/документации** — в основном репо, только код и публичные доки. Никогда не класть рабочие записи в корень репо.

### Обновление architecture.md — когда:
- Добавлен или удален модуль
- Изменен data flow (новый источник данных, новый слой кэширования, новый transport)
- Изменена DuckDB-схема (новые таблицы, изменение типов)
- Добавлена новая группа tools (не отдельный tool в существующую группу)
- Изменен способ аутентификации или подключения к API

Не обновлять при: баг-фиксах, новых preset-ах в существующей группе, изменениях тестов.

### Обновление README.md — когда:
- Изменилось количество tools (обновить таблицу)
- Изменился способ установки или запуска
- Добавлены новые env-переменные
- Изменились ограничения или поддерживаемые тарифы
- Проект опубликован / появился новый способ подключения

Не обновлять при: внутренних рефакторингах, изменениях тестов, обновлениях зависимостей.

## Структура проекта

```
src/appmetrica_mcp/
  server.py       ← 16 tools, lifespan, все MCP-определения
  client.py       ← HTTP-клиент (Management + Reporting + Logs API)
  cache.py        ← DuckDB кэш сырых событий/установок
  analytics.py    ← воронки, retention, когорты (чистые SQL-функции)
  metrics.py      ← каталог ym:ge: метрик + preset-отчеты
  models.py       ← Pydantic-модели ответов
  config.py       ← конфигурация из env
tests/
  conftest.py     ← фикстуры: _make_event(), _make_install(), in-memory DuckDB
  test_client.py  ← respx-моки HTTP
  test_cache.py   ← DuckDB store/query
  test_analytics.py ← воронки, retention, когорты
  test_tools.py   ← валидация presets и каталога метрик
```

## Правила

### Абсолютные
- **Read-only.** Никаких write-операций в AppMetrica. Никогда.
- **Не логировать токен.** MCP работает через stdio — stdout = протокол.
- **Не добавлять зависимости** без явной необходимости. pandas, numpy — не нужны.
- **Тесты обязательны** для нового кода. `uv run pytest` должен проходить до коммита.

### Код
- `from __future__ import annotations` в каждом модуле
- Tools: `Annotated[type, "description"]` для параметров, `annotations=READ_ONLY` обязательно
- SQL: CTE-based, `::TIMESTAMP` cast для datetime, `UBIGINT` для device_id
- Ошибки: `AppMetricaError` → `{"error": ..., "status_code": ...}` dict, не исключения на уровне tool

### AppMetrica API
- Management: `api.appmetrica.yandex.com`
- Reporting + Logs: `api.appmetrica.yandex.ru`
- Auth: `Authorization: OAuth {token}` header
- Logs API: async (202 → poll 10s → 200), timeout 5 min
- Rate: 30 req/sec, 5000 req/day

### DuckDB-схема
- Определена в `cache.py`, откалибрована по реальным CSV-экспортам
- Events: 33 поля, Installations: 43 поля
- `appmetrica_device_id` — UBIGINT (значения > INT64)
- Datetime хранится как VARCHAR, кастится в TIMESTAMP при аналитических запросах
