from __future__ import annotations

import duckdb


def build_funnel(
    db: duckdb.DuckDBPyConnection,
    steps: list[dict],
    date_from: str,
    date_to: str,
    country: str | None = None,
    source: str | None = None,
    time_window_hours: int = 48,
) -> dict:
    """Build a sequential event funnel from cached raw events.

    Each step is {"event_name": str, "param_filters": {key: val} | None}.
    Returns per-step user counts and conversion rates.
    """
    if not steps:
        return {"steps": [], "total_entered": 0, "total_completed": 0, "overall_conversion": 0.0}

    ctes: list[str] = []
    for i, step in enumerate(steps):
        event_name = step["event_name"].replace("'", "''")
        param_filters = step.get("param_filters") or {}

        conditions = [
            f"event_name = '{event_name}'",
            f"event_datetime >= '{date_from}'",
            f"event_datetime <= '{date_to} 23:59:59'",
        ]

        if country:
            conditions.append(f"country_iso_code = '{country.replace(chr(39), chr(39)*2)}'")

        for key, val in param_filters.items():
            safe_key = key.replace("'", "''")
            safe_val = val.replace("'", "''")
            conditions.append(
                f"json_extract_string(event_json, '$.{safe_key}') = '{safe_val}'"
            )

        where = " AND ".join(conditions)

        if i == 0:
            ctes.append(f"""step_{i} AS (
    SELECT appmetrica_device_id, MIN(event_datetime::TIMESTAMP) AS step_time
    FROM events
    WHERE {where}
    GROUP BY appmetrica_device_id
)""")
        else:
            prev = f"step_{i - 1}"
            ctes.append(f"""step_{i} AS (
    SELECT e.appmetrica_device_id, MIN(e.event_datetime::TIMESTAMP) AS step_time
    FROM events e
    JOIN {prev} p ON e.appmetrica_device_id = p.appmetrica_device_id
    WHERE {where}
      AND e.event_datetime::TIMESTAMP >= p.step_time
      AND e.event_datetime::TIMESTAMP <= p.step_time + INTERVAL '{time_window_hours} hours'
    GROUP BY e.appmetrica_device_id
)""")

    if source:
        safe_source = source.replace("'", "''")
        source_cte = f"""source_filter AS (
    SELECT DISTINCT appmetrica_device_id
    FROM installations
    WHERE tracker_name ILIKE '%{safe_source}%'
)"""
        ctes.insert(0, source_cte)
        old_first = ctes[1]
        ctes[1] = old_first.replace(
            "GROUP BY appmetrica_device_id",
            "AND appmetrica_device_id IN (SELECT appmetrica_device_id FROM source_filter)\n    GROUP BY appmetrica_device_id",
        )

    count_selects = [f"(SELECT COUNT(*) FROM step_{i})" for i in range(len(steps))]
    count_query = f"WITH {', '.join(ctes)}\nSELECT {', '.join(count_selects)}"

    row = db.execute(count_query).fetchone()
    if not row:
        return {"steps": [], "total_entered": 0, "total_completed": 0, "overall_conversion": 0.0}

    counts = list(row)
    total_entered = counts[0] if counts else 0

    result_steps = []
    for i, step in enumerate(steps):
        users = counts[i]
        conv_start = (users / total_entered * 100) if total_entered > 0 else 0.0
        prev_users = counts[i - 1] if i > 0 else total_entered
        conv_prev = (users / prev_users * 100) if prev_users > 0 else 0.0
        drop_off = prev_users - users if i > 0 else 0

        result_steps.append({
            "step": i + 1,
            "event_name": step["event_name"],
            "users": users,
            "conversion_from_start": round(conv_start, 2),
            "conversion_from_prev": round(conv_prev, 2),
            "drop_off": drop_off,
        })

    total_completed = counts[-1] if counts else 0
    overall = (total_completed / total_entered * 100) if total_entered > 0 else 0.0

    return {
        "steps": result_steps,
        "total_entered": total_entered,
        "total_completed": total_completed,
        "overall_conversion": round(overall, 2),
    }


def compute_retention(
    db: duckdb.DuckDBPyConnection,
    days: list[int],
    cohort_from: str,
    cohort_to: str,
    country: str | None = None,
    source: str | None = None,
) -> list[dict]:
    """Compute retention from raw install + event data.

    Returns list of {cohort_date, cohort_size, retention: {day: pct}}.
    """
    cohort_conditions = [
        f"install_datetime::DATE BETWEEN '{cohort_from}' AND '{cohort_to}'",
        "is_reinstallation = false",
    ]
    if country:
        cohort_conditions.append(f"country_iso_code = '{country.replace(chr(39), chr(39)*2)}'")
    if source:
        safe_source = source.replace("'", "''")
        cohort_conditions.append(f"tracker_name ILIKE '%{safe_source}%'")

    cohort_where = " AND ".join(cohort_conditions)
    days_list = ", ".join(str(d) for d in days)

    sql = f"""
    WITH cohort AS (
        SELECT appmetrica_device_id,
               install_datetime::DATE AS cohort_date
        FROM installations
        WHERE {cohort_where}
    ),
    cohort_sizes AS (
        SELECT cohort_date, COUNT(DISTINCT appmetrica_device_id) AS cohort_size
        FROM cohort
        GROUP BY cohort_date
    ),
    returns AS (
        SELECT c.cohort_date,
               c.appmetrica_device_id,
               (e.event_datetime::DATE - c.cohort_date)::INT AS days_since_install
        FROM cohort c
        JOIN events e ON c.appmetrica_device_id = e.appmetrica_device_id
        WHERE (e.event_datetime::DATE - c.cohort_date)::INT IN ({days_list})
    )
    SELECT
        cs.cohort_date::VARCHAR AS cohort_date,
        cs.cohort_size,
        r.days_since_install,
        COUNT(DISTINCT r.appmetrica_device_id) AS returned_users
    FROM cohort_sizes cs
    LEFT JOIN returns r ON cs.cohort_date = r.cohort_date
    GROUP BY cs.cohort_date, cs.cohort_size, r.days_since_install
    ORDER BY cs.cohort_date, r.days_since_install
    """

    rows = db.execute(sql).fetchall()

    cohorts: dict[str, dict] = {}
    for cohort_date, cohort_size, day, returned in rows:
        if cohort_date not in cohorts:
            cohorts[cohort_date] = {
                "cohort_date": cohort_date,
                "cohort_size": cohort_size,
                "retention": {},
            }
        if day is not None and cohort_size > 0:
            cohorts[cohort_date]["retention"][day] = round(
                returned / cohort_size * 100, 2
            )

    return sorted(cohorts.values(), key=lambda x: x["cohort_date"])


def compare_cohorts(
    db: duckdb.DuckDBPyConnection,
    cohort_a_filters: dict,
    cohort_b_filters: dict,
    metrics: list[str],
    date_from: str,
    date_to: str,
) -> list[dict]:
    """Compare two user cohorts on given metrics.

    Supported metrics: retention_d{N}, avg_sessions, total_events, user_count.
    """
    results = []

    for metric in metrics:
        val_a = _compute_cohort_metric(db, cohort_a_filters, metric, date_from, date_to)
        val_b = _compute_cohort_metric(db, cohort_b_filters, metric, date_from, date_to)
        diff = val_a - val_b
        diff_pct = (diff / val_b * 100) if val_b != 0 else 0.0

        results.append({
            "metric": metric,
            "cohort_a_value": round(val_a, 2),
            "cohort_b_value": round(val_b, 2),
            "difference": round(diff, 2),
            "difference_pct": round(diff_pct, 2),
        })

    return results


def _build_cohort_where(filters: dict, date_from: str, date_to: str) -> str:
    conditions = [
        f"install_datetime::DATE BETWEEN '{date_from}' AND '{date_to}'",
        "is_reinstallation = false",
    ]
    if "country" in filters:
        safe = filters["country"].replace("'", "''")
        conditions.append(f"country_iso_code = '{safe}'")
    if "source" in filters:
        safe = filters["source"].replace("'", "''")
        conditions.append(f"tracker_name ILIKE '%{safe}%'")
    return " AND ".join(conditions)


def _compute_cohort_metric(
    db: duckdb.DuckDBPyConnection,
    filters: dict,
    metric: str,
    date_from: str,
    date_to: str,
) -> float:
    cohort_where = _build_cohort_where(filters, date_from, date_to)

    if metric.startswith("retention_d"):
        day = int(metric.replace("retention_d", ""))
        sql = f"""
        WITH cohort AS (
            SELECT appmetrica_device_id, install_datetime::DATE AS cohort_date
            FROM installations WHERE {cohort_where}
        )
        SELECT
            COUNT(DISTINCT CASE
                WHEN (e.event_datetime::DATE - c.cohort_date)::INT = {day}
                THEN e.appmetrica_device_id END) * 100.0
            / NULLIF(COUNT(DISTINCT c.appmetrica_device_id), 0)
        FROM cohort c
        LEFT JOIN events e ON c.appmetrica_device_id = e.appmetrica_device_id
        """
        row = db.execute(sql).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    if metric == "avg_sessions":
        sql = f"""
        WITH cohort AS (
            SELECT appmetrica_device_id
            FROM installations WHERE {cohort_where}
        )
        SELECT COUNT(DISTINCT e.session_id) * 1.0 / NULLIF(COUNT(DISTINCT c.appmetrica_device_id), 0)
        FROM cohort c
        LEFT JOIN events e ON c.appmetrica_device_id = e.appmetrica_device_id
        """
        row = db.execute(sql).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    if metric == "total_events":
        sql = f"""
        WITH cohort AS (
            SELECT appmetrica_device_id
            FROM installations WHERE {cohort_where}
        )
        SELECT COUNT(*)
        FROM cohort c
        JOIN events e ON c.appmetrica_device_id = e.appmetrica_device_id
        """
        row = db.execute(sql).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    if metric == "user_count":
        sql = f"""
        SELECT COUNT(DISTINCT appmetrica_device_id)
        FROM installations WHERE {cohort_where}
        """
        row = db.execute(sql).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    return 0.0
