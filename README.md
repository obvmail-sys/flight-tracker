# Трекер цен на авиабилеты

Сайт: https://obvmail-sys.github.io/flight-tracker/ — поля «Откуда / Куда»,
точечный график «дата вылета → цена» с линейным трендом и таблица цен.

Данные: Travelpayouts Aviasales Data API, `aviasales/v3/prices_for_dates`, EUR
(кэш Aviasales 2–7 дней).

## config.json

```json
{
  "routes": [ { "origin": "HEL", "destination": "BCN" } ],
  "horizon_days_min": 60,
  "horizon_days_max": 90,
  "trip_days": 3
}
```

- `routes` — отслеживаемые маршруты (IATA-коды). Новый маршрут появится на сайте
  после следующего сбора.
- `horizon_days_min..max` — окно дат вылета: от сегодня+60 до сегодня+90 дней.
- `trip_days` — длина поездки: возврат = вылет + 3 дня.

## Как устроено

- `fetch.py` (Python 3.10+, без зависимостей) — для каждого маршрута:
  - **roundtrip** — туда-обратно одним билетом (`one_way=false`);
  - **two_oneway** — два билета в одну сторону (`one_way=true`), сумма цен.
- `data/prices.json` — история цен. Одна запись = поездка + цена; если та же цена
  встречается снова, обновляется только `last_seen_at`. Файл пишется атомарно,
  при ошибках API не трогается.
- `data/last_run.json` — статус последнего запуска (в т.ч. ошибки).
- `.github/workflows/fetch.yml` — запуск каждые 4 часа и вручную, коммит данных.

Сайт статический, поэтому живого запроса к API из браузера нет (токен стал бы
публичным) — он показывает данные, собранные по расписанию.

Локально: `TRAVELPAYOUTS_TOKEN=... python3 fetch.py`, затем `python3 -m http.server`.
