# AppMetrica MCP Server

Read-only MCP-сервер для аналитики мобильных и web-игр через [AppMetrica](https://appmetrica.yandex.com/). Подключается к Claude Code, Claude Desktop, Cursor и другим MCP-совместимым клиентам.

## Зачем

Позволяет задавать аналитические вопросы на естественном языке и получать ответы напрямую из AppMetrica:

- *«Покажи DAU за последнюю неделю по странам»*
- *«Построй воронку level_start → level_finish для US по Facebook»*
- *«Какой retention D3/D1 у когорты 1-7 мая по Applovin?»*
- *«Сравни retention когорт facebook vs organic»*

Сервер сам формирует API-запросы, кэширует сырые данные в DuckDB, считает воронки и retention из raw events.

## Как работает

Три слоя данных:

1. **Reporting API** — агрегированные метрики (DAU, retention, revenue, crashes). Быстрые ответы, без локального хранения.
2. **Logs API** — сырые события и установки. Асинхронный экспорт (202 → poll → 200), результат кэшируется в DuckDB.
3. **Analytics engine** — воронки, deep retention, сравнение когорт. SQL-вычисления поверх DuckDB кэша.

## Быстрый старт

```bash
# 1. Получить OAuth-токен AppMetrica
#    https://oauth.yandex.com/ → создать приложение → права appmetrica:read

# 2. Установить токен
export APPMETRICA_OAUTH_TOKEN=your_token

# 3. Установить зависимости
uv sync

# 4. Проверить что работает
uv run pytest
```

MCP-сервер автоматически подхватывается Claude Code через `.claude/settings.json`.

Для ручного запуска: `uv run python -m appmetrica_mcp`

## 16 tools

| Группа | Tools | Источник |
|--------|-------|----------|
| Discovery | `list_apps`, `list_metrics` | Management API |
| Universal | `get_report`, `get_drilldown` | Reporting API |
| Presets | `get_traffic`, `get_retention`, `get_revenue`, `get_crashes`, `get_geo`, `get_events`, `get_devices`, `get_versions` | Reporting API |
| Advanced | `build_funnel_report`, `compare_cohorts_report`, `get_deep_retention`, `sync_events` | Logs API + DuckDB |

## Архитектура (кратко)

```
server.py          — FastMCP, 16 tools, lifespan
  ├── client.py    — HTTP-клиент (Management + Reporting + Logs API)
  ├── cache.py     — DuckDB кэш сырых событий/установок
  ├── analytics.py — воронки, retention, когорты (SQL поверх DuckDB)
  ├── metrics.py   — каталог метрик + preset-отчеты
  ├── models.py    — Pydantic-модели ответов
  └── config.py    — конфигурация из env
```

Подробнее: [architecture.md](./architecture.md)

## Ограничения

- Read-only: нет Post API, Push API, write-операций
- Имена некоторых метрик Reporting API (`ym:ge:retention1Day` и т.д.) не верифицированы — при 400 ошибке API сам подскажет правильное имя
- Кэш DuckDB append-only (возможны дубликаты при повторной синхронизации одного диапазона)
- Logs API может готовить данные 1-5 минут при первом запросе
