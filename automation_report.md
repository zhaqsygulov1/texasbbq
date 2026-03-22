## Lots 2025 extraction report

- Source TRU codes sheet: `1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k`
- Destination sheet: `13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU`
- Filters used on goszakup:
  - `filter[year]=2025`
  - `filter[status][]=360` (Закупка состоялась)
  - `filter[amount_from]=15000000`

### Final result

- Rows written to destination (excluding header): **111,453**
- Columns in destination:
  1. № лота
  2. Код ТРУ
  3. Наименование товара
  4. Наименование объявления
  5. Наименование и описание лота
  6. Кол-во
  7. Сумма, тг.
  8. Способ закупки
  9. Статус

### Sanity checks

- All statuses in final table: `Закупка состоялась`
- Minimal amount in final table: `15,000,000.00`

