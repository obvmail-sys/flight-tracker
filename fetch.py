#!/usr/bin/env python3
"""Сбор цен HEL ⇄ BCN из Travelpayouts Aviasales Data API (prices_for_dates).

Пишет:
  data/prices.json   — история цен (без дублей)
  data/last_run.json — статус последнего запуска (время, число запросов, ошибки)

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
    "type", "departure_date", "return_date", "airline_out", "airline_back",
    "transfers", "transfers_back", "price_eur",
)

log = logging.getLogger("fetch")


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def add_months(d: date, n: int) -> date:
    """Первое число месяца через n месяцев от месяца даты d."""
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def month_end(d: date) -> date:
    return add_months(d, 1) - timedelta(days=1)


def ym(d: date) -> str:
    return d.strftime("%Y-%m")


def months_between(start: date, end: date) -> list[date]:
    """Первые числа всех месяцев, пересекающих отрезок [start, end]."""
    cur, out = date(start.year, start.month, 1), []
    while cur <= end:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def departure_months(today: date, months_ahead: int) -> list[date]:
    """Текущий месяц и следующие: всего months_ahead месяцев."""
    return [add_months(today, i) for i in range(months_ahead)]


def return_months_for(dep_month: date, dmin: int, dmax: int,
                      today: date | None = None) -> list[date]:
    """Месяцы возврата, покрывающие окно поездки dmin..dmax для любого дня
    месяца вылета. Обычно это 2-й и 3-й месяц после вылета
    (2027-01 → 2027-03, 2027-04); для вылетов в конце месяца при 90 днях
    окно может задеть ещё и 1-е число 4-го месяца — его тоже берём."""
    start = max(dep_month, today) if today else dep_month
    first = start + timedelta(days=dmin)
    last = month_end(dep_month) + timedelta(days=dmax)
    return months_between(first, last)


def iso_date(s: str | None) -> str | None:
    return s[:10] if s else None


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
        self.cfg = cfg
        self.today = today
        self.dmin = int(cfg["trip_days_min"])
        self.dmax = int(cfg["trip_days_max"])
        self.requests = 0
        self.errors: list[str] = []

    def query(self, **params) -> list[dict] | None:
        self.requests += 1
        desc = " ".join(f"{k}={v}" for k, v in params.items())
        try:
            rows = api_get(self.token, params)
            log.info("OK  %-70s → %d", desc, len(rows))
            return rows
        except ApiError as e:
            msg = f"{desc}: {e}"
            log.error("ERR %s", msg)
            self.errors.append(msg)
            return None
        finally:
            time.sleep(REQUEST_PAUSE_S)

    def _trip_ok(self, dep: str | None, ret: str | None) -> int | None:
        if not dep or not ret:
            return None
        d, r = date.fromisoformat(dep), date.fromisoformat(ret)
        if d < self.today:
            return None
        days = (r - d).days
        return days if self.dmin <= days <= self.dmax else None

    # A) туда-обратно одним билетом
    def roundtrips(self) -> list[dict]:
        o, dst = self.cfg["origin"], self.cfg["destination"]
        out = []
        for dm in departure_months(self.today, int(self.cfg["months_ahead"])):
            for rm in return_months_for(dm, self.dmin, self.dmax, self.today):
                rows = self.query(origin=o, destination=dst, departure_at=ym(dm),
                                  return_at=ym(rm), one_way="false")
                for r in rows or []:
                    dep, ret = iso_date(r.get("departure_at")), iso_date(r.get("return_at"))
                    days = self._trip_ok(dep, ret)
                    if days is None or r.get("price") is None:
                        continue
                    out.append({
                        "type": "roundtrip",
                        "departure_date": dep,
                        "return_date": ret,
                        "trip_days": days,
                        "price_eur": round(float(r["price"]), 2),
                        "airline_out": r.get("airline"),
                        "airline_back": r.get("airline"),
                        "transfers": r.get("transfers"),
                        "transfers_back": r.get("return_transfers"),
                        "link": r.get("link"),
                    })
        return out

    # B) два билета в одну сторону
    def two_oneway(self) -> list[dict]:
        o, dst = self.cfg["origin"], self.cfg["destination"]
        dep_months = departure_months(self.today, int(self.cfg["months_ahead"]))
        ret_months = sorted({m for dm in dep_months
                             for m in return_months_for(dm, self.dmin, self.dmax, self.today)})

        def cheapest_per_day(rows: list[dict]) -> dict[str, dict]:
            best: dict[str, dict] = {}
            for r in rows:
                d = iso_date(r.get("departure_at"))
                if d and r.get("price") is not None and (
                        d not in best or r["price"] < best[d]["price"]):
                    best[d] = r
            return best

        outbound: dict[str, dict] = {}
        for m in dep_months:
            outbound.update(cheapest_per_day(self.query(
                origin=o, destination=dst, departure_at=ym(m), one_way="true") or []))
        inbound: dict[str, dict] = {}
        for m in ret_months:
            inbound.update(cheapest_per_day(self.query(
                origin=dst, destination=o, departure_at=ym(m), one_way="true") or []))

        # Все допустимые пары (вылет + возврат через dmin..dmax дней).
        pairs = []
        for dep, a in outbound.items():
            for ret, b in inbound.items():
                days = self._trip_ok(dep, ret)
                if days is not None:
                    pairs.append((dep, ret, days, a, b))

        # Чтобы файл не разрастался на десятки тысяч комбинаций, оставляем
        # лучшую пару для каждой даты вылета и для каждой даты возврата.
        best_dep: dict[str, tuple] = {}
        best_ret: dict[str, tuple] = {}
        for p in pairs:
            total = p[3]["price"] + p[4]["price"]
            if p[0] not in best_dep or total < best_dep[p[0]][0]:
                best_dep[p[0]] = (total, p)
            if p[1] not in best_ret or total < best_ret[p[1]][0]:
                best_ret[p[1]] = (total, p)
        chosen = {(p[0], p[1]): p for _, p in (*best_dep.values(), *best_ret.values())}

        out = []
        for dep, ret, days, a, b in chosen.values():
            out.append({
                "type": "two_oneway",
                "departure_date": dep,
                "return_date": ret,
                "trip_days": days,
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
            # ссылки могут обновляться — держим актуальные
            for f in ("link", "link_back"):
                if r.get(f):
                    index[k][f] = r[f]
            continue
        rec = {"checked_at": checked_at, **r, "last_seen_at": checked_at}
        index[k] = rec
        existing.append(rec)
        added += 1
    existing.sort(key=lambda r: (r["departure_date"], r["return_date"], r["type"],
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

    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    checked_at = now.isoformat().replace("+00:00", "Z")

    try:
        existing = load_prices(PRICES_PATH)
    except (ValueError, json.JSONDecodeError) as e:
        log.error("data/prices.json повреждён, не трогаю его: %s", e)
        return 3

    c = Collector(token, cfg, now.date())
    fresh = c.roundtrips() + c.two_oneway()
    failed_all = c.errors and len(c.errors) == c.requests

    status = {
        "checked_at": checked_at,
        "ok": not c.errors,
        "requests": c.requests,
        "errors": c.errors,
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
