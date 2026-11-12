---
owner: пользователь (владелец ИФТ-проверки)
updated: 2026-11-12
---

# Проверка fix-ift-category-create-flow

## Подтверждённая причина

Readonly-клиент Google с scope spreadsheets.readonly прочитал только ИФТ-книгу `1jaEzmJPqYzxSp-HtDw-zwt_kPqSEs4enBrkG-Lc-UzA`. На исходном коде подготовка create завершилась ValueError `An indexed archive sheet is missing` в category_changes.prepare_change. Категории «Одежда» в реестре в момент проверки не было.

После исправления на настоящем снимке прошли prepare_change и CategoryManager.preview для «Одежда» с четырьмя подкатегориями владельца; приватный план сериализован, существующий сводный архив неизменен. Применение плана не запускалось; Google-записей, новых категорий и финансовых операций probe не делал. Промышленная книга не читалась и не изменялась.

## Автоматические проверки

- 155 Python-тестов — OK. Включены регрессии dangling archive для create/add/move/deactivate и строгого rename, native create/resume с сохранением индекса, объяснимая ошибка без записи, UI close после ошибки/успеха/pending.
- Реальный PTB Application/ConversationHandler и Update с in-memory Bot API: одна и четыре подкатегории → preview → confirm, повтор confirm без дубля, close с сохранением успеха; ошибка → повтор → отмена; переход в аналитику без применения черновика. Telegram/Sheets-сеть этими тестами не вызывается.
- `node tests/test_apps_script.cjs` и `node tests/test_archive_script.cjs` — OK.
- `openspec validate --all --strict` — 11 items passed.
- `git diff --check` — OK.

## Область выпуска

Только feature/manage-categories и test-тег через Test stand. Main/origin/main: `77c4d221610d3d78d0ed70bfa03384aa4feda156`; runtime production Compose/workflow, main.py и Apps Script в исправлении не изменяются. Ранее подготовленные спецификации окружений включаются в feature как документация, не как рабочий выпуск.

## Проверка владельцем после test deploy

`/categories` → «Создать категорию» → «Расход» → «Одежда» → список из четырёх строк. Должен появиться предпросмотр; при отмене категория не создаётся, при подтверждении появляется в справочнике и выборе новой операции. Можно также проверить одну подкатегорию. Старые архивы не восстанавливать и индекс вручную не чистить ради этой проверки. Полная приёмка UI остаётся у владельца.
