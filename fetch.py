#!/usr/bin/env python3
"""Сбор цен на авиабилеты в одну сторону из Travelpayouts Aviasales Data API
(prices_for_dates, one_way=true).

Для каждого маршрута из config.json запрашиваются оба направления
(origin → destination и обратно) на вылеты в окне
[сегодня + collect_days_min, сегодня + collect_days_max].

Пишет:
  data/prices.json   — история цен (без дублей)
  data/last_run.json — статус последнего запуска (время, запросы, ошибки)

Токен берётся из переменной окружения TRAVELPAYOUTS_TOKEN.
Только стандартная библиотека — зависимостей нет.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PRICES_PATH = ROOT / "data" / "prices.json"
LAST_RUN_PATH = ROOT / "data" / "last_run.json"

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
PAGE_LIMIT = 1000  # максимум, который отдаёт API
MAX_PAGES = 5
REQUEST_PAUSE_S = 0.3
RETRIES = 3

# Поля, по которым запись считается той же самой (цена входит в ключ:
# новая цена = новая запись истории).
KEY_FIELDS = (
    "origin", "destination", "departure_date", "departure_time",
    "airline", "flight_number", "transfers", "price_eur",
)

log = logging.getLogger("fetch")


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def add_months(d: date, n: int) -> date:
    """Первое число месяца через n месяцев от месяца даты d."""
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def ym(d: date) -> str:
    return d.strftime("%Y-%m")


def months_between(start: date, end: date) -> list[date]:
    """Первые числа всех месяцев, пересекающих отрезок [start, end]."""
    cur, out = date(start.year, start.month, 1), []
    while cur <= end:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def iso_date(s: str | None) -> str | None:
    return s[:10] if s else None


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text("utf-8"))
    if "routes" not in cfg:  # старый формат: один маршрут в корне
        cfg["routes"] = [{"origin": cfg["origin"], "destination": cfg["destination"]}]
    for r in cfg["routes"]:
        r["origin"], r["destination"] = r["origin"].upper(), r["destination"].upper()
    cfg.setdefault("collect_days_min", 0)
    cfg.setdefault("collect_days_max", 90)
    cfg.setdefault("view_days_min", 60)
    cfg.setdefault("view_days_max", 90)
    return cfg


# ---------------------------------------------------------------- API

def api_get(token: str, params: dict) -> list[dict]:
    """Все страницы ответа prices_for_dates. Бросает ApiError при ошибке."""
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        q = dict(params, currency="eur", sorting="price", unique="false",
                 limit=PAGE_LIMIT, page=page)
        url = f"{API_URL}?{urllib.parse.urlencode(q)}"
        payload = _request_json(url, token)
        if not payload.get("success", False):
            raise ApiError(f"API success=false: {payload.get('error') or payload}")
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise ApiError(f"неожиданный формат data: {type(data).__name__}")
        rows.extend(data)
        if len(data) < PAGE_LIMIT:
            break
        time.sleep(REQUEST_PAUSE_S)
    return rows


def _request_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "X-Access-Token": token,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "flight-tracker/1.0",
    })
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last_err = ApiError(f"HTTP {e.code}: {body}")
            if e.code in (400, 401, 403, 404):
                break  # повтор не поможет
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = ApiError(f"{type(e).__name__}: {e}")
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise last_err or ApiError("unknown error")


# ---------------------------------------------------------------- collect

class Collector:
    def __init__(self, token: str, cfg: dict, today: date):
        self.token = token
        self.dep_from = today + timedelta(days=int(cfg["collect_days_min"]))
        self.dep_to = today + timedelta(days=int(cfg["collect_days_max"]))
        self.requests = 0
        self.errors: list[str] = []

    def query(self, **params) -> list[dict] | None:
        self.requests += 1
        desc = " ".join(f"{k}={v}" for k, v in params.items())
        try:
            rows = api_get(self.token, params)
            log.info("OK  %-60s → %d", desc, len(rows))
            return rows
        except ApiError as e:
            msg = f"{desc}: {e}"
            log.error("ERR %s", msg)
            self.errors.append(msg)
            return None
        finally:
            time.sleep(REQUEST_PAUSE_S)

    def oneway(self, o: str, dst: str) -> list[dict]:
        out = []
        for m in months_between(self.dep_from, self.dep_to):
            for r in self.query(origin=o, destination=dst, departure_at=ym(m),
                                one_way="true") or []:
                dep_at = r.get("departure_at") or ""
                dep = iso_date(dep_at)
                if (not dep or r.get("price") is None
                        or not self.dep_from <= date.fromisoformat(dep) <= self.dep_to):
                    continue
                out.append({
                    "origin": o, "destination": dst,
                    "departure_date": dep,
                    "departure_time": dep_at[11:16] or None,
                    "price_eur": round(float(r["price"]), 2),
                    "airline": r.get("airline"),
                    "flight_number": str(r.get("flight_number") or "") or None,
                    "transfers": r.get("transfers"),
                    "duration_min": r.get("duration_to") or r.get("duration"),
                    "link": r.get("link"),
                })
        return out


# ---------------------------------------------------------------- storage

def record_key(r: dict) -> tuple:
    return tuple(r.get(f) for f in KEY_FIELDS)


def load_prices(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: ожидался JSON-массив")
    return data


def merge(existing: list[dict], fresh: list[dict], checked_at: str) -> tuple[list[dict], int]:
    """Добавляет новые записи; для уже известных (тот же рейс-день и та же цена)
    только обновляет last_seen_at. Возвращает (записи, число добавленных)."""
    index = {record_key(r): r for r in existing}
    added = 0
    for r in fresh:
        k = record_key(r)
        if k in index:
            index[k]["last_seen_at"] = checked_at
            if r.get("link"):  # ссылки могут обновляться
                index[k]["link"] = r["link"]
            continue
        rec = {"checked_at": checked_at, **r, "last_seen_at": checked_at}
        index[k] = rec
        existing.append(rec)
        added += 1
    existing.sort(key=lambda r: (r["origin"], r["destination"], r["departure_date"],
                                 r["price_eur"], r["checked_at"]))
    return existing, added


def atomic_write_json(path: Path, obj, one_per_line: bool = False) -> None:
    """Пишет во временный файл и атомарно подменяет — исходный файл
    никогда не остаётся полузаписанным."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if one_per_line and isinstance(obj, list):
        body = "[\n" + ",\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":"))
                                  for x in obj) + ("\n]\n" if obj else "]\n")
    else:
        body = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    json.loads(body)  # проверка валидности перед записью
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- main

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    token = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
    if not token:
        log.error("TRAVELPAYOUTS_TOKEN не задан")
        return 2

    cfg = load_config(CONFIG_PATH)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    checked_at = now.isoformat().replace("+00:00", "Z")

    try:
        existing = load_prices(PRICES_PATH)
    except (ValueError, json.JSONDecodeError) as e:
        log.error("data/prices.json повреждён, не трогаю его: %s", e)
        return 3

    c = Collector(token, cfg, now.date())
    log.info("Окно вылета %s … %s", c.dep_from, c.dep_to)
    fresh: list[dict] = []
    for route in cfg["routes"]:
        fresh += c.oneway(route["origin"], route["destination"])
        fresh += c.oneway(route["destination"], route["origin"])
    failed_all = bool(c.errors) and len(c.errors) == c.requests

    status = {
        "checked_at": checked_at,
        "ok": not c.errors,
        "requests": c.requests,
        "errors": c.errors,
        "window": {"from": c.dep_from.isoformat(), "to": c.dep_to.isoformat()},
        "found": len(fresh),
        "config": cfg,
    }

    if failed_all:
        log.error("Все %d запросов завершились ошибкой — prices.json не изменён", c.requests)
        status["added"] = 0
        atomic_write_json(LAST_RUN_PATH, status)
        return 1

    merged, added = merge(existing, fresh, checked_at)
    atomic_write_json(PRICES_PATH, merged, one_per_line=True)
    status["added"] = added
    status["total_records"] = len(merged)
    atomic_write_json(LAST_RUN_PATH, status)
    log.info("Найдено %d вариантов, новых записей %d, всего %d, ошибок %d",
             len(fresh), added, len(merged), len(c.errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
