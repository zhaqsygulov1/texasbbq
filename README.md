# Сбор лотов госзакупок (2025)

Скрипт `scripts/goszakup_lots_2025.py`:

1. читает коды ТРУ из исходной Google-таблицы;
2. парсит `goszakup.gov.kz` по фильтрам;
3. формирует CSV с колонками:
   - `№ лота`
   - `Код ТРУ`
   - `Наименование товара`
   - `Наименование объявления`
   - `Наименование и описание лота`
   - `Кол-во`
   - `Сумма, тг.`
   - `Способ закупки`
   - `Статус`

## Быстрый запуск

```bash
python3 scripts/goszakup_lots_2025.py
```

Результаты:

- `output/lots_2025.csv`
- `output/lots_2025_warnings.json`

## Полезные опции

```bash
python3 scripts/goszakup_lots_2025.py \
  --workers 6 \
  --year 2025 \
  --status 360 \
  --amount-from 15000000 \
  --count-record 2000
```

Тест на первых 10 кодах:

```bash
python3 scripts/goszakup_lots_2025.py --limit-codes 10
```
