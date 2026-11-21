#!/usr/bin/env python3
"""Сбор цен на авиабилеты из Travelpayouts Aviasales Data API (prices_for_dates).

Для каждого маршрута из config.json проверяются вылеты в окне
[сегодня + horizon_days_min, сегодня + horizon_days_max] и поездки ровно
на trip_days дней (возврат = вылет + trip_days):
  A) туда-обратно одним билетом (one_way=false);
  B) два билета в одну сторону (one_way=true), сумма цен.

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
    "origin", "destination", "type", "departure_date", "return_date",
    "airline_out", "airline_back", "transfers", "transfers_back", "price_eur",
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
    cfg.setdefault("horizon_days_min", 60)
    cfg.setdefault("horizon_days_max", 90)
    cfg.setdefault("trip_days", 3)
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
        self.trip = int(cfg["trip_days"])
        self.dep_from = today + timedelta(days=int(cfg["horizon_days_min"]))
        self.dep_to = today + timedelta(days=int(cfg["horizon_days_max"]))
        self.requests = 0
        self.errors: list[str] = []

    @property
    def dep_months(self) -> list[date]:
        return months_between(self.dep_from, self.dep_to)

    @property
    def ret_months(self) -> list[date]:
        return months_between(self.dep_from + timedelta(days=self.trip),
                              self.dep_to + timedelta(days=self.trip))

    def query(self, **params) -> list[dict] | None:
        self.requests += 1
        desc = " ".join(f"{k}={v}" for k, v in params.items())
        try:
            rows = api_get(self.token, params)
            log.info("OK  %-75s → %d", desc, len(rows))
            return rows
        except ApiError as e:
            msg = f"{desc}: {e}"
            log.error("ERR %s", msg)
            self.errors.append(msg)
            return None
        finally:
            time.sleep(REQUEST_PAUSE_S)

    def _in_window(self, dep: str | None) -> bool:
        return bool(dep) and self.dep_from <= date.fromisoformat(dep) <= self.dep_to

    def collect(self, origin: str, destination: str) -> list[dict]:
        return self.roundtrips(origin, destination) + self.two_oneway(origin, destination)

    # A) туда-обратно одним билетом
    def roundtrips(self, o: str, dst: str) -> list[dict]:
        out = []
        for dm in self.dep_months:
            # месяц возврата — тот же или следующий (поездка короткая,
            # ограничение API «не больше 30 дней между вылетом и возвратом»
            # не мешает)
            for rm in (m for m in self.ret_months if m in (dm, add_months(dm, 1))):
                for r in self.query(origin=o, destination=dst, departure_at=ym(dm),
                                    return_at=ym(rm), one_way="false") or []:
                    dep, ret = iso_date(r.get("departure_at")), iso_date(r.get("return_at"))
                    if (not self._in_window(dep) or not ret or r.get("price") is None
                            or (date.fromisoformat(ret) - date.fromisoformat(dep)).days != self.trip):
                        continue
                    out.append({
                        "origin": o, "destination": dst,
                        "type": "roundtrip",
                        "departure_date": dep,
                        "return_date": ret,
                        "trip_days": self.trip,
                        "price_eur": round(float(r["price"]), 2),
                        "airline_out": r.get("airline"),
                        "airline_back": r.get("airline"),
                        "transfers": r.get("transfers"),
                        "transfers_back": r.get("return_transfers"),
                        "link": r.get("link"),
                    })
        return out

    # B) два билета в одну сторону
    def two_oneway(self, o: str, dst: str) -> list[dict]:
        def cheapest_per_day(rows: list[dict]) -> dict[str, dict]:
            best: dict[str, dict] = {}
            for r in rows:
                d = iso_date(r.get("departure_at"))
                if d and r.get("price") is not None and (
                        d not in best or r["price"] < best[d]["price"]):
                    best[d] = r
            return best

        outbound: dict[str, dict] = {}
        for m in self.dep_months:
            outbound.update(cheapest_per_day(self.query(
                origin=o, destination=dst, departure_at=ym(m), one_way="true") or []))
        inbound: dict[str, dict] = {}
        for m in self.ret_months:
            inbound.update(cheapest_per_day(self.query(
                origin=dst, destination=o, departure_at=ym(m), one_way="true") or []))

        out = []
        for dep, a in sorted(outbound.items()):
            if not self._in_window(dep):
                continue
            ret = (date.fromisoformat(dep) + timedelta(days=self.trip)).isoformat()
            b = inbound.get(ret)
            if not b:
                continue
            out.append({
                "origin": o, "destination": dst,
                "type": "two_oneway",
                "departure_date": dep,
                "return_date": ret,
                "trip_days": self.trip,
                "price_eur": round(float(a["price"]) + float(b["price"]), 2),
                "price_out_eur": round(float(a["price"]), 2),
                "price_back_eur": round(float(b["price"]), 2),
                "airline_out": a.get("airline"),
                "airline_back": b.get("airline"),
                "transfers": a.get("transfers"),
                "transfers_back": b.get("transfers"),
                "link": a.get("link"),
                "link_back": b.get("link"),
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
            for f in ("link", "link_back"):  # ссылки могут обновляться
                if r.get(f):
                    index[k][f] = r[f]
            continue
        rec = {"checked_at": checked_at, **r, "last_seen_at": checked_at}
        index[k] = rec
        existing.append(rec)
        added += 1
    existing.sort(key=lambda r: (r.get("origin", ""), r.get("destination", ""),
                                 r["departure_date"], r["type"], r["price_eur"],
                                 r["checked_at"]))
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
    log.info("Окно вылета %s … %s, поездка %d дн.", c.dep_from, c.dep_to, c.trip)
    fresh: list[dict] = []
    for route in cfg["routes"]:
        fresh += c.collect(route["origin"], route["destination"])
    failed_all = bool(c.errors) and len(c.errors) == c.requests

    status = {
        "checked_at": checked_at,
        "ok": not c.errors,
        "requests": c.requests,
        "errors": c.errors,
        "window": {"from": c.dep_from.isoformat(), "to": c.dep_to.isoformat()},
        "found": len(fresh),
        "found_roundtrip": sum(r["type"] == "roundtrip" for r in fresh),
        "found_two_oneway": sum(r["type"] == "two_oneway" for r in fresh),
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
