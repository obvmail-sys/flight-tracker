# Трекер цен HEL ⇄ BCN

Отслеживает цены туда-обратно Хельсинки ⇄ Барселона для поездки на 60–90 дней
(Travelpayouts Aviasales Data API, `aviasales/v3/prices_for_dates`, EUR).

- `config.json` — маршрут, горизонт (`months_ahead`), длина поездки.
- `fetch.py` — сбор цен (только стандартная библиотека Python 3.10+):
  - **roundtrip** — `one_way=false`, для каждого месяца вылета запрашиваются месяцы
    интервал вылет→возврат больше 30 дней), остаются поездки на 60–90 дней;
  - **two_oneway** — `one_way=true` HEL→BCN и BCN→HEL, из них собираются пары
    вылет + возврат через 60–90 дней, цена = сумма. Сохраняется лучшая пара
    для каждой даты вылета и для каждой даты возврата.
- `data/prices.json` — история цен. Одна запись = поездка + цена; если та же цена
  встречается снова, обновляется только `last_seen_at`. Файл пишется атомарно,
  при ошибках API не трогается.
- `data/last_run.json` — статус последнего запуска (в т.ч. ошибки).
- `index.html` — страница с лучшими ценами, графиками и таблицей.
- `.github/workflows/fetch.yml` — запуск каждые 4 часа и вручную, коммит данных.

## Настройка

1. Settings → Secrets and variables → Actions → `TRAVELPAYOUTS_TOKEN`.
2. Settings → Pages → Deploy from a branch → ветка с данными, папка `/ (root)`.
3. Actions → **Fetch prices** → Run workflow (первый запуск вручную).

Локально: `TRAVELPAYOUTS_TOKEN=... python3 fetch.py`, затем `python3 -m http.server`
и открыть http://localhost:8000.
