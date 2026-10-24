ticktick-list: 64ecf570ba9bd10472b02b9b — действия и сроки владельца из этой работы заносит скилл ticktick-tasks
ticktick-tag: dev — тег области на каждом пункте этого проекта

# CLAUDE.md

## Проект

AppMetrica CLI — read-only Python-инструмент для game analytics поверх AppMetrica API. CLI-first: общее ядро (`service.py`) используют и терминал (`appmetrica`), и MCP-сервер (`appmetrica mcp`). MCP — тонкий адаптер, а не отдельная реализация. Сырые данные лежат в Parquet-партициях, из которых пересобирается локальная `analytics.duckdb`.

## Быстрый старт разработки

```bash
uv sync --extra dev          # установить зависимости
uv run pytest -v             # запустить тесты
uv run appmetrica auth login # сохранить токен в системное хранилище
uv run appmetrica --help     # список команд
uv run appmetrica mcp        # запустить MCP-сервер по stdio
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
src/appmetrica_cli/
  cli.py          ← click-команды `appmetrica`: auth, sync, build, отчёты, sql, mcp
  server.py       ← FastMCP: Reporting- и локальные тулы, 2 prompt
  service.py      ← общее ядро: sync, build, локальные отчёты, reconcile
  client.py       ← HTTP-клиент Management + Reporting + Logs API
  loader.py       ← синхронизация Logs API в партиции
  storage.py      ← Parquet-партиции, state.json, сборка analytics.duckdb
  db.py           ← read-only подключение к analytics.duckdb
  report.py       ← DAU, sessions, installs, retention, IAP и ad revenue
  funnel.py       ← воронки на уровне устройства
  sandbox.py      ← read-only SQL с таймаутом и лимитом строк
  discover.py     ← список событий и их параметров
  fx.py           ← курсы ECB и конвертация в USD
  metrics.py      ← каталог Reporting-метрик и пресетов
  config.py       ← конфигурация из env, home_dir()
  auth.py         ← токен: OS-хранилище, env, защищённый файл
  __main__.py     ← запуск через python -m appmetrica_cli
tests/
  conftest.py     ← общие фикстуры
  helpers.py      ← синтетические партиции и сборка store
  test_*.py       ← по модулям: auth, cli, client, discover, funnel, fx,
                    mcp_tools, report, sandbox, service, storage, tools
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
- SQL: CTE-based; значения — только через `?`; время и день — готовые `ts_utc`/`day` из `build()`, сырые колонки — VARCHAR
- Ошибки: `AppMetricaError` → `{"error": ..., "status_code": ...}` dict, не исключения на уровне tool

### AppMetrica API
- Management: `api.appmetrica.yandex.com`
- Reporting + Logs: `api.appmetrica.yandex.ru`
- Auth: `Authorization: OAuth {token}` header
- Logs API: async (202 → poll 10s → 200), timeout 5 min
- Rate: 30 req/sec, 5000 req/day

### analytics.duckdb
- Собирается `AppStore.build()` из Parquet-партиций в `<home>/apps/<id>/analytics.duckdb` и публикуется атомарно
- Исходные колонки — VARCHAR ровно в том виде, как их вернул API; IP-колонки не запрашиваются
- Вычисляемые колонки: `ts_device_utc`, `ts_receive_utc`, `ts_utc` (эффективное время D2: device, если оно не больше 5 минут впереди и не больше 7 дней позади receive, иначе receive), `time_fixed`, `day`, `receive_day`
- `day` и `receive_day` — дни в часовом поясе приложения; `app_meta` хранит `app_id` и `time_zone`
- Отчёты открывают базу read-only; после `sync` нужен `build`
