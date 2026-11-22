#!/usr/bin/env python3
"""Сбор цен на авиабилеты в одну сторону из Travelpayouts Aviasales Data API
(prices_for_dates, one_way=true).

Вкладки сайта описаны файлами routes/*.json:
  {"name": "Барселона", "from": ["HEL"], "to": ["BCN", "GRO", "REU"]}
Для каждой пары аэропортов (from × to) собираются оба направления на вылеты
в окне [сегодня + collect_days_min, сегодня + collect_days_max] (config.json).

Пишет:
  data/legs/{FROM}-{TO}.json — история цен по направлению (без дублей)
  data/last_run.json         — статус запуска: вкладки, коды, найдено, ошибки
  data/airports.json         — компактный справочник аэропортов для сайта

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
ROUTES_DIR = ROOT / "routes"
LEGS_DIR = ROOT / "data" / "legs"
OLD_PRICES_PATH = ROOT / "data" / "prices.json"   # старый формат (один файл) — мигрируется
LAST_RUN_PATH = ROOT / "data" / "last_run.json"
AIRPORTS_PATH = ROOT / "data" / "airports.json"
REF_URL = "https://api.travelpayouts.com/data/ru/{}.json"
REF_MAX_AGE_DAYS = 7

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
    cfg = json.loads(path.read_text("utf-8")) if path.exists() else {}
    cfg.setdefault("collect_days_min", 0)
    cfg.setdefault("collect_days_max", 180)
    return cfg


def load_tabs(cfg: dict) -> list[dict]:
    """Вкладки из routes/*.json (id = имя файла). Если папки нет — из config.json."""
    tabs = []
    for f in sorted(ROUTES_DIR.glob("*.json")) if ROUTES_DIR.exists() else []:
        try:
            t = json.loads(f.read_text("utf-8"))
            codes = lambda v: [c.strip().upper() for c in (v if isinstance(v, list) else [v]) if str(c).strip()]
            tabs.append({"id": f.stem, "name": str(t.get("name") or f.stem),
                         "from": codes(t.get("from", [])), "to": codes(t.get("to", []))})
        except Exception as e:  # битый файл вкладки не должен ломать остальные
            tabs.append({"id": f.stem, "name": f.stem, "from": [], "to": [], "error": f"не читается: {e}"})
    if not tabs:
        for r in cfg.get("routes", []):
            o, d = r["origin"].upper(), r["destination"].upper()
            tabs.append({"id": f"{o}-{d}".lower(), "name": f"{o} ⇄ {d}", "from": [o], "to": [d]})
    return tabs


def load_reference() -> dict[str, list]:
    """Справочник аэропортов: code → [код, аэропорт, город, страна, lat, lon].
    Раз в неделю обновляется с Travelpayouts (без токена) и сохраняется для сайта."""
    old = {}
    if AIRPORTS_PATH.exists():
        try:
            old = json.loads(AIRPORTS_PATH.read_text("utf-8"))
        except Exception:
            old = {}
    built = old.get("built_at", "")
    fresh_enough = built and (datetime.now(timezone.utc) - datetime.fromisoformat(built.replace("Z", "+00:00"))).days < REF_MAX_AGE_DAYS
    if not fresh_enough:
        try:
            def get(name):
                req = urllib.request.Request(REF_URL.format(name), headers={"User-Agent": "flight-tracker/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8"))
            countries = {c.get("code"): (c.get("name") or (c.get("name_translations") or {}).get("en") or c.get("code"))
                         for c in get("countries")}
            cities = {c.get("code"): (c.get("name") or (c.get("name_translations") or {}).get("en") or c.get("code"))
                      for c in get("cities")}
            rows = []
            for a in get("airports"):
                code = a.get("code")
                if not code or len(code) != 3 or a.get("flightable") is False:
                    continue
                if a.get("iata_type") not in (None, "airport"):
                    continue
                co = a.get("coordinates") or {}
                rows.append([code,
                             a.get("name") or (a.get("name_translations") or {}).get("en") or code,
                             cities.get(a.get("city_code"), a.get("city_code") or ""),
                             countries.get(a.get("country_code"), a.get("country_code") or ""),
                             round(co.get("lat") or 0, 2), round(co.get("lon") or 0, 2)])
            rows.sort()
            old = {"built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                   "airports": rows}
            atomic_write_json(AIRPORTS_PATH, old, one_per_line=False, compact=True)
            log.info("Справочник аэропортов обновлён: %d", len(rows))
        except Exception as e:
            log.warning("Справочник аэропортов недоступен (%s) — использую сохранённый", e)
    return {r[0]: r for r in old.get("airports", [])}


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
        # API отдаёт на каждую дату только самый дешёвый вариант — обычно с
        # пересадкой. Поэтому отдельно запрашиваем и самые дешёвые прямые.
        for m in months_between(self.dep_from, self.dep_to):
            for direct in ("false", "true"):
                rows = self.query(origin=o, destination=dst, departure_at=ym(m),
                                  one_way="true", direct=direct) or []
                for r in rows:
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

def leg_path(o: str, d: str) -> Path:
    return LEGS_DIR / f"{o}-{d}.json"


def migrate_old_prices() -> None:
    """Однократно раскладывает старый data/prices.json по файлам направлений."""
    if not OLD_PRICES_PATH.exists():
        return
    try:
        old = load_prices(OLD_PRICES_PATH)
    except Exception as e:
        log.warning("Старый prices.json не читается (%s) — пропускаю миграцию", e)
        return
    by_leg: dict[tuple, list] = {}
    for r in old:
        if r.get("origin") and r.get("destination") and "airline" in r:
            by_leg.setdefault((r["origin"], r["destination"]), []).append(r)
    for (o, d), rows in by_leg.items():
        if not leg_path(o, d).exists():
            atomic_write_json(leg_path(o, d), rows, one_per_line=True)
    OLD_PRICES_PATH.unlink()
    log.info("Миграция: prices.json → %d файлов направлений", len(by_leg))


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


def atomic_write_json(path: Path, obj, one_per_line: bool = False, compact: bool = False) -> None:
    """Пишет во временный файл и атомарно подменяет — исходный файл
    никогда не остаётся полузаписанным."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if one_per_line and isinstance(obj, list):
        body = "[\n" + ",\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":"))
                                  for x in obj) + ("\n]\n" if obj else "]\n")
    elif compact:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
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
    tabs = load_tabs(cfg)
    ref = load_reference()
    migrate_old_prices()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    checked_at = now.isoformat().replace("+00:00", "Z")

    # проверка кодов и список уникальных направлений
    legs: list[tuple[str, str]] = []
    for t in tabs:
        bad = [c for c in t["from"] + t["to"] if len(c) != 3 or not c.isalpha() or (ref and c not in ref)]
        t["unknown"] = bad
        good_from = [c for c in t["from"] if c not in bad]
        good_to = [c for c in t["to"] if c not in bad]
        t["legs"] = []
        for o in good_from:
            for d in good_to:
                if o == d:
                    continue
                for leg in ((o, d), (d, o)):
                    t["legs"].append(f"{leg[0]}-{leg[1]}")
                    if leg not in legs:
                        legs.append(leg)

    c = Collector(token, cfg, now.date())
    log.info("Окно вылета %s … %s; вкладок %d, направлений %d", c.dep_from, c.dep_to, len(tabs), len(legs))
    leg_stat: dict[str, dict] = {}
    total_found = total_added = 0
    for o, d in legs:
        req0, err0 = c.requests, len(c.errors)
        fresh = c.oneway(o, d)
        n_req, n_err = c.requests - req0, len(c.errors) - err0
        st = {"found": len(fresh), "errors": n_err}
        if n_req and n_err == n_req:
            st["failed"] = True  # все запросы по направлению упали — файл не трогаем
        else:
            try:
                existing = load_prices(leg_path(o, d))
            except (ValueError, json.JSONDecodeError) as e:
                log.error("%s повреждён, не трогаю его: %s", leg_path(o, d).name, e)
                st["failed"] = True
                existing = None
            if existing is not None:
                merged, added = merge(existing, fresh, checked_at)
                atomic_write_json(leg_path(o, d), merged, one_per_line=True)
                st["added"], st["total"] = added, len(merged)
                total_added += added
        total_found += len(fresh)
        leg_stat[f"{o}-{d}"] = st

    for t in tabs:
        t["found"] = {k: leg_stat.get(k, {}).get("found", 0) for k in t["legs"]}
        t["failed_legs"] = [k for k in t["legs"] if leg_stat.get(k, {}).get("failed")]

    used = sorted({x for t in tabs for x in t["from"] + t["to"]})
    status = {
        "checked_at": checked_at,
        "ok": not c.errors,
        "requests": c.requests,
        "errors": c.errors,
        "window": {"from": c.dep_from.isoformat(), "to": c.dep_to.isoformat()},
        "found": total_found,
        "added": total_added,
        "config": {k: v for k, v in cfg.items() if k != "routes"},
        "tabs": tabs,
        "legs": leg_stat,
        "names": {x: ref[x][1:4] for x in used if x in ref},  # код → [аэропорт, город, страна]
    }
    atomic_write_json(LAST_RUN_PATH, status)
    failed_all = bool(c.errors) and len(c.errors) == c.requests
    log.info("Найдено %d вариантов, новых записей %d, запросов %d, ошибок %d",
             total_found, total_added, c.requests, len(c.errors))
    return 1 if failed_all else 0


if __name__ == "__main__":
    sys.exit(main())
