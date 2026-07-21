# Журнал выполнения Fantasy Analytics

Историческая часть плана разработки. Здесь хранятся подробные карточки
выполнения завершённых шагов (даты, ветки, commit/PR, команды проверок и их
результаты, фактический результат, решения и отклонения) и общий журнал
изменений статусов.

Планирование, текущие статусы и ключевые нюансы каждого шага — в
[`docs/development-plan.md`](development-plan.md). При завершении шага агент
записывает сюда карточку выполнения и добавляет строку в журнал обновлений.

## Карточки выполнения

### Шаг 0. Discovery-прототип и локальный Docker

Фактический результат:

- Работает CLI для live GraphQL discovery.
- Подтверждены 16 клубов, 30 туров, 240 матчей и 590 fantasy-записей игроков
  сезона 2025/2026.
- Добавлены `schema/postgres.sql`, Dockerfile и Compose с PostgreSQL 18.
- Зафиксированы ограничения API в `docs/data-model.md`.
- Проходят 13 unit-тестов.

Карточка выполнения:

- Начат: 2026-07-19
- Завершён: 2026-07-19
- Commit: `ac9be65`
- Проверки: unit-тесты, live discovery, статическая проверка Compose.
- Отклонения: Docker daemon отсутствовал в агентском окружении; полный Compose
  запуск должен быть подтверждён локально.

### Шаг 1. Persistence layer и миграции

Фактический результат:

- Модели SQLAlchemy 2 (`src/fantasy_analytics/db/models.py`) — единственный
  авторитетный источник схемы; файл `schema/postgres.sql` удалён.
- Настроен Alembic; начальная миграция `0001` материализует метаданные моделей
  (применяется и откатывается). Проверено `compare_metadata`: расхождений нет.
- Добавлены engine/session helpers, `session_scope` и конфигурация
  `DATABASE_URL` (`db/config.py`).
- `IngestionRepository` (`db/repository.py`) транзакционно управляет статусами
  `ingestion_runs` и идемпотентно пишет `raw_api_responses` (dedup по SHA-256
  хешу ответа и unique-констрейнту).
- Добавлены CLI `fantasy-migrate` и сервис `migrate` в Compose/Make; чистая БД
  создаётся одной командой `make migrate` (или `fantasy-migrate upgrade`).

Карточка выполнения:

- Начат: 2026-07-20
- Завершён: 2026-07-20
- Агент/ветка: `cursor/persistence-layer-migrations-bd72`
- Commit/PR: `0b9d2d2`
- Проверки:
  - `python -m fantasy_analytics.db.cli upgrade/downgrade` на PostgreSQL 16 —
    18 таблиц создаются и полностью удаляются.
  - `fantasy-migrate upgrade` на чистой БД создаёт схему одной командой.
  - `compare_metadata(models, db)` — 0 расхождений (DDL и модели совпадают).
  - `python -m unittest discover -s tests` — 24 теста проходят (с БД);
    без БД 9 интеграционных тестов корректно пропускаются.
  - `python -m build --wheel` — миграции и mako попадают в дистрибутив.
- Решения и отклонения:
  - Начальная миграция использует `metadata.create_all/drop_all` вместо
    статичных `op.create_table`, чтобы гарантировать единый источник схемы и
    исключить дрейф между ORM и DDL. Последующие миграции должны использовать
    `alembic revision --autogenerate`.
  - Драйвер — psycopg 3 (`postgresql+psycopg://`).
  - Docker daemon в окружении агента отсутствовал; интеграция проверена на
    локально установленном PostgreSQL 16, полный запуск Compose должен быть
    подтверждён локально (аналогично шагу 0).

### Шаг 2. Полный исторический импорт

Фактический результат:

- Добавлен двухстадийный импортёр (`src/fantasy_analytics/ingestion.py`):
  стадия fetch скачивает все страницы (турнир, сезон, все страницы игроков,
  клубные сезонные агрегаты, историю матчей каждого игрока с минутами) с
  retry/backoff клиента и ограниченным пулом потоков; стадия persist пишет всё
  в одной транзакции.
- `src/fantasy_analytics/db/import_repository.py` — идемпотентные bulk-upsert'ы
  каталога по внешним идентификаторам и вставка run-scoped снапшотов.
- Добавлены CLI `fantasy-ingest` и консольная точка входа с выбором сезона
  (`--season-id`, `--season-name`, `--current`, `--history-workers`).
- Отчёт запуска содержит счётчики по всем сущностям и длительность каждой
  стадии; каждый запуск учитывается в `ingestion_runs` с версионированным
  отчётом.

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/full-historical-import-d9c2`
- Commit/PR: PR #4
- Проверки:
  - Боевой импорт сезона 2025/2026 в чистую БД: 16 клубов, 16 season_clubs,
    30 туров, 240 матчей, 590 игроков и player_seasons, 9578 player_match_stats,
    480 club_match_stats, 16 club_season_stats; history загружена для 423
    игроков с минутами, `skipped_history_matches = 0`. Стадии: fetch ~27.3s,
    persist ~4.0s.
  - Идемпотентность: повторный запуск не изменил каталог (clubs 16, matches 240,
    player_seasons 590, player_match_stats 9578, club_match_stats 480), а
    run-scoped снапшоты выросли на одно поколение (fantasy_player_snapshots
    590→1180, player_season_stats 590→1180, club_season_stats 16→32).
  - Сверка данных: все 240 матчей имеют счёт и тур; сумма ролей = 590; агрегат
    топ-игрока (Сперцян: 225 очков, 13 голов, 16 передач, 2543 минуты) совпадает
    с `gameStat` из API; club_match_stats = 240 домашних + 240 гостевых.
  - `python -m unittest discover -s tests` — 33 теста проходят (юнит + БД),
    включая интеграционные тесты идемпотентности и «сбой не публикует
    частичный snapshot».
- Решения и отклонения:
  - Команды в `season.tours.matches.home/away.team.id` приходят как stat-слаги,
    а в истории игрока `match.team.id` — как fantasy-id; импортёр строит оба
    маппинга (stat_team_id→club, fantasy_team_id→season_club).
  - Ингест-ран фиксируется отдельной транзакцией от доменного snapshot: при
    сбое запуск помечается `failed`, а доменная транзакция откатывается целиком,
    не публикуя частичных данных.
  - История загружается только для игроков с `fieldMinutes > 0` (423 из 590):
    у игроков без минут история пуста, лишние запросы исключены.
  - `fantasy_point_details` пусты: `statDetails` в завершённых матчах приходит
    пустым (подтверждено в шаге 0), маппинг реализован и активируется, когда
    поле начнёт заполняться. Расширенная `statMatch`-статистика — вне объёма.

### Шаг 3. Исследование расширенной match-статистики

Фактический результат:

- Добавлена операция `statQueries.football.match(id)` (`queries.py`,
  `build_match_stats_query`) с флагами наличия, командной и по-игроцкой
  статистикой, составами и лентой событий.
- Модуль `src/fantasy_analytics/match_stats.py`: воспроизводимая выборка
  матчей (равномерно по сезону + покрытие всех клубов), измерение заполненности
  каждого GraphQL-пути, классификация решений (`ADOPT`/`CONDITIONAL`/`SPARSE`/
  `EXCLUDE`) и сверка со счётом каталога.
- CLI `fantasy-match-stats` (`match_stats_cli.py`) читает матчи из БД, пишет raw
  fixtures, `field-coverage.json/md`, `match-consistency.json` и `report.json`.
- Снимок таблицы покрытия зафиксирован в `docs/match-stats-coverage.md`; выводы
  и маппинг добавлены в `docs/data-model.md`.

Критерии приёмки (выполнено):

- Таблица `GraphQL path → тип → заполненность → решение` — `docs/match-stats-coverage.md`.
- Запросы воспроизводимы одной CLI-командой `fantasy-match-stats`.
- Ненадёжные и отсутствующие поля явно помечены `SPARSE`/`EXCLUDE`.
- Идентификаторы связи Fantasy↔stat документированы (stat-слаги команд и игроков).
- Добавлены контрактные тесты выбранной операции (fake-transport + live по флагу).

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/step3-match-stats-spike-e44d`
- Commit/PR: PR по ветке `cursor/step3-match-stats-spike-e44d`
- Проверки:
  - Боевой прогон `fantasy-match-stats --season-name 2025/2026 --sample-size 40`:
    40 матчей, 16 клубов, 30 туров; `hasDetailStat/hasLineups/hasEvents/
    hasPersonStat` = 40/40, `hasXG` = 10/40; сверка счёта с каталогом 40/40;
    11 стартовых игроков у каждой стороны 40/40. 50 из 113 полей `ADOPT`.
  - `python -m unittest discover -s tests` — 45 тестов (44 + 1 live-skip)
    проходят; live-контракт подтверждён `RUN_LIVE_MATCH_STATS=1`.
- Решения и отклонения:
  - Матч резолвится через `statQueries.football.match(id)` по
    `matches.stat_match_id` без аргумента `source`; корневой `match(ID)`
    относится к контент-матчу и отдаёт `NOT_FOUND` для stat-id.
  - xG для РПЛ ненадёжен (`hasXG` только у части матчей, поля пусты) — вынесен в
    опциональные и не адаптируется.
  - По-игроцкая статистика скудная: надёжны только голы, карточки, автоголы и
    созданные моменты; пасовые/дуэльные разбивки и xG исключены.
  - Схема БД в этом шаге не менялась (спайк-исследование), предложение по
    хранению расширенной статистики описано в `docs/data-model.md`.

### Шаг 4. Контроль качества и reconciliation

Фактический результат:

- Добавлена таблица `data_quality_issues` (severity `blocking`/`warning`,
  `expected`/`actual`, `details`) и поля `season_id`, `is_active`,
  `quality_checked_at` в `ingestion_runs`. Частичный уникальный индекс
  `ingestion_runs_active_season_idx` гарантирует не более одного активного
  run на сезон.
- Модуль `src/fantasy_analytics/quality.py`: формализованный набор проверок
  (`catalog_completeness`, `reference_integrity`, `duplicate_fixtures`,
  `match_score_completeness`, `club_result_reconciliation`,
  `player_points_reconciliation`). Reconciliation сверяет клубные сезонные
  агрегаты с результатами из `club_match_stats` и сезонный fantasy-итог игрока
  с суммой его матчевой статистики. Расхождения по матчам внутри 72-часового
  окна корректировки понижаются с `blocking` до `warning`.
- `QualityRepository` (`db/quality_repository.py`) идемпотентно перезаписывает
  issues run'а и публикует snapshot: при отсутствии blocking — помечает run
  активным и деактивирует прочие активные run'ы сезона; при наличии blocking —
  оставляет неактивным, не вытесняя последний валидный snapshot.
- CLI `fantasy-quality` (`quality_cli.py`) печатает JSON-отчёт с ожидаемым и
  фактическим значением каждой проверки и возвращает код 1 при blocking.

Критерии приёмки (выполнено):

- Формализованный набор blocking/warning проверок — таблица в
  `docs/data-model.md`, раздел «Data quality gate (step 4)».
- Некорректный snapshot не становится активным: `is_active` выставляется только
  при 0 blocking (подтверждено на реальных данных и integration-тестами).
- Отчёт показывает ожидаемое и фактическое значение каждой проверки (поля
  `expected`/`actual` в отчёте и в `data_quality_issues`).
- Fixtures покрывают пропущенную страницу (игрок с минутами без истории →
  blocking), дубль ID (повтор fixture → blocking) и изменение результата
  (рассинхрон агрегата → blocking вне окна, warning внутри 72ч).

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/step4-data-quality-reconciliation-a55e`
- Commit/PR: PR по ветке `cursor/step4-data-quality-reconciliation-a55e`
- Проверки:
  - `python -m unittest discover -s tests` — 57 тестов проходят (было 45),
    1 live-skip; добавлен `tests/test_quality.py` (12 тестов: unit + сценарии
    missing page, duplicate id, result change и окно 72ч).
  - Миграции: чистая цепочка `base → 0001 → 0002` создаёт 18 таблиц,
    `compare_metadata(models, db)` = 0 расхождений; `downgrade`/`upgrade`
    по ревизиям проходят.
  - Боевой прогон на сезоне 2025/2026 (16/30/240, 590 игроков, 9578 match-stats):
    `fantasy-quality` — PASSED, 0 blocking, 0 warning, snapshot активен;
    `club_result_reconciliation` сверил 16 клубов (0 mismatch),
    `player_points_reconciliation` — 590 игроков (0 missing_history, 0 mismatch).
  - Негативный боевой прогон: внедрён дубликат матча → gate BLOCKED (exit 1),
    issue записан (`expected=1`, `actual=2`), `is_active=false`; после удаления
    дубликата повторный прогон — PASSED, snapshot снова активен, issues очищены.
- Решения и отклонения:
  - Начальная миграция `0001` переписана со «живого» `metadata.create_all`
    на статичный явный baseline (снимок схемы после шага 2). Прежний подход
    делал невозможной любую аддитивную миграцию: `create_all` уже создавал новую
    таблицу/колонки, и `0002` падал с `DuplicateTable`. Модели по-прежнему —
    единственный источник текущей схемы; последующие миграции используют
    `alembic revision --autogenerate` (как и предписано в карточке шага 1).
  - Reconciliation по-игрокам и по-клубам на завершённом сезоне сходится точно,
    поэтому blocking-строгость безопасна (нет ложных срабатываний). Внутри окна
    72ч рассинхроны понижаются до warning.
  - `data_quality_issues` перезаписывается целиком на каждый прогон (delete+
    insert), обеспечивая идемпотентность.

### Шаг 5. Ручной ingestion job и backend-команда

Фактический результат:

- Добавлена таблица `ingestion_jobs` (модель `IngestionJob`, миграция `0003`):
  статусы `pending/running/succeeded/failed`, ссылка `ingestion_run_id` на
  порождённый импорт, `result` (JSONB) и `error_message`. Частичный уникальный
  индекс `ingestion_jobs_active_tournament_idx` гарантирует не более одного
  активного (`pending`/`running`) задания на турнир.
- `IngestionJobRepository` (`db/job_repository.py`) идемпотентно ставит задание
  в очередь (возвращает существующее активное вместо дубликата) и переводит его
  по жизненному циклу; хелпер `advisory_lock_key` детерминированно отображает
  слуг турнира в ключ advisory-lock.
- Worker `fantasy-ingestion-worker` (`ingestion_worker.py`) исполняется
  **отдельным процессом**: берёт session-level `pg_try_advisory_lock` по турниру,
  помечает задание `running`, запускает импорт (шаг 2) и затем gate качества
  (шаг 4), публикующий snapshot только при 0 blocking, и записывает объединённый
  `result` (счётчики импорта, отчёт качества, `data_freshness`,
  `snapshot_active`) либо безопасное сообщение об ошибке (редакция кредов).
- FastAPI-приложение `fantasy-api` (`api.py`): `POST /admin/ingestion/rpl/refresh`
  ставит задание в очередь, сразу возвращает `202` с id и запускает worker
  отдельным `subprocess` (`start_new_session=True`); при активном задании
  возвращает `409`. `GET /admin/ingestion/runs/{id}` читает задание из БД.
  `GET /health` — проба готовности. Тело запроса позволяет выбрать сезон
  (`season_id`/`season_name`/`current`), по умолчанию — последний завершённый.

Критерии приёмки (выполнено):

- API сразу возвращает `202` и ID задания — боевой POST вернул `202` за 0.03s.
- Одновременно не более одного refresh турнира — второй POST во время импорта
  вернул `409`; защита на двух уровнях (частичный unique-индекс + advisory lock,
  проверено тестом на удержании чужого lock).
- Статусы `pending/running/succeeded/failed` переживают перезапуск API —
  после Ctrl-C и повторного старта `GET /runs/1` вернул `succeeded`.
- Ошибка содержит безопасное диагностическое сообщение — креды в строке
  подключения редактируются (`safe_error_message`, unit-тест).
- Успешный запуск обновляет время актуальности — `result.data_freshness`
  устанавливается в `finished_at` опубликованного run.

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/step5-manual-ingestion-job-7eae`
- Commit/PR: PR #7
- Проверки:
  - `python -m unittest discover -s tests` — 72 теста проходят (было 57),
    1 live-skip; добавлены `tests/test_api.py` (9) и
    `tests/test_ingestion_worker.py` (6): успех/сбой worker, mutual exclusion по
    advisory lock, пропуск не-`pending` задания, `202`/`409`/`404`, персистентность
    статуса после «перезапуска» API, e2e refresh с реальным импортом.
  - Миграции: цепочка `0002 → 0003` создаёт `ingestion_jobs`,
    `downgrade`/`upgrade` проходят, `compare_metadata` = 0 расхождений.
  - Боевой e2e через `fantasy-api` + worker-subprocess на живом API Sports.ru:
    `POST /admin/ingestion/rpl/refresh` → `202` (0.03s), задание прошло
    `pending → running → succeeded` за ~46s, импортировано 16 клубов, 30 туров,
    240 матчей, 590 игроков, 9578 player_match_stats; gate качества — 0 blocking,
    0 warning, `ingestion_runs.is_active = true`, `data_freshness` заполнен.
    Параллельный POST во время импорта → `409`; после Ctrl-C + рестарта API
    `GET /runs/1` = `succeeded` (статус переживает перезапуск).
- Решения и отклонения:
  - Введена отдельная таблица `ingestion_jobs` (админ-уровень) поверх
    `ingestion_runs` (внутренний импорт): задание ссылается на порождённый run
    через `ingestion_run_id`. Путь `GET /admin/ingestion/runs/{id}` принимает id
    задания (возвращаемый refresh-эндпоинтом): «run» здесь трактуется как
    админ-уровневый refresh-run.
  - Worker запускается как самостоятельный `subprocess` (не `multiprocessing`),
    отвязанный через `start_new_session=True`, чтобы импорт переживал рестарт API.
    Для тестируемости ядро вынесено в импортируемую `execute_job`, а запуск
    worker'а в API инъектируется (`spawn_worker`).
  - Redis не используется: взаимное исключение — частичный unique-индекс +
    session-level advisory lock (`pg_try_advisory_lock`, AUTOCOMMIT-соединение,
    освобождается в `finally`).

### Шаг 6. Аналитические признаки

Фактический результат:

- Модуль `src/fantasy_analytics/features.py` строит воспроизводимый,
  leakage-free dataset для целевого тура из **активного** snapshot (или
  `--run-id`). Все признаки считаются строго по матчам, стартовавшим до дедлайна
  тура (`cutoff`); матчи самого целевого тура дополнительно исключаются из
  истории как вторая защита от неверно проставленного дедлайна. Модуль только
  читает БД, не ходит в Sports.ru API и ничего не пишет.
- Реализованы: rolling-метрики за 3/5/10 матчей (очки, голы, ассисты, минуты,
  число появлений), per-90 показатели, доля появлений и стартов
  (`field_minutes >= 60`), домашняя/гостевая сила атаки и защиты клубов и
  соперника, дни отдыха, статус доступности из snapshot, а также **отдельно**
  оценённые вероятность выхода (`p_appearance`) и ожидаемые минуты
  (`expected_minutes`).
- Каждая строка помечена `feature_version`, `player` (`player_season_id`/
  `fantasy_player_id`), `tour` и `tour_cutoff`. Стратегия заполнения пропусков
  явная и документирована.
- CLI `fantasy-features` (`features_cli.py`) строит dataset одной командой и
  пишет `features.json` (метаданные + словарь признаков + строки),
  `features.csv` и `feature-dictionary.json`; в stdout — компактная сводка.
- Словарь признаков и стратегия пропусков зафиксированы в
  `docs/feature-dictionary.md`; краткое описание — в `README.md` и
  `docs/data-model.md`. Схема БД в этом шаге не менялась.

Критерии приёмки (выполнено):

- Dataset строится одной командой для указанного тура — `fantasy-features
  --tour <id>`.
- Каждая строка имеет `player`, `tour`, cutoff time и версию признаков.
- Unit-тесты подтверждают rolling windows и отсутствие будущих матчей
  (`recent_before_cutoff`, интеграционный тест «только история до cutoff» и
  «матч целевого тура не утекает в историю»).
- Пропуски имеют явную стратегию заполнения (таблица в
  `docs/feature-dictionary.md`).
- Словарь признаков документирован (`docs/feature-dictionary.md` +
  `FEATURE_DICTIONARY` в отчёте).

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/step6-analytical-features-5fc2`
- Commit/PR: PR по ветке `cursor/step6-analytical-features-5fc2`
- Проверки:
  - `python -m unittest discover -s tests` — 82 теста проходят (было 72),
    1 live-skip; добавлен `tests/test_features.py` (10 тестов: чистые
    rolling/per-90/strength-хелперы + интеграционные сценарии leakage,
    идентичности строк, выбора активного snapshot и ошибок резолвинга).
  - Боевой прогон на активном snapshot сезона 2025/2026 (run 1):
    `fantasy-features --tour 1786` (15 тур, cutoff 2025-11-08T11:00Z) построил
    590 строк по 8 фикстурам, 16 клубов с историей, 0 игроков без фикстуры.
  - Проверка отсутствия leakage по БД: у `player_season_id=1` (Сперцян) ровно
    14 матчей до cutoff (= `total_appearances`) и 16 после (исключены);
    ни у одной строки `appearances_5 > 5` и `p_appearance > 1`.
  - Обработка краевых случаев на боевых данных: 21 строка с `INJURY` →
    `p_appearance=0`, `expected_minutes=0`; 212 строк без истории →
    нулевые rolling/per-90 при fallback силы клуба к среднему по лиге.
- Решения и отклонения:
  - Шаг 6 не добавляет таблиц в БД: dataset воспроизводим из
    `(run_id, tour, feature_version)` и пишется артефактами на диск; это
    удовлетворяет критериям и не создаёт риска миграций. Шаг 7 может вызвать
    `build_feature_dataset` напрямую in-process.
  - «Старт» аппроксимируется порогом `field_minutes >= 60`, так как явный флаг
    состава в БД не импортируется (разведка составов — спайк шага 3).
  - `cutoff` = `transfers_deadline_at` тура (fallback: `starts_at`, затем
    ранний kickoff фикстуры). Для завершённого сезона авто-выбор «следующего
    тура» намеренно требует явного `--tour` (нужно бэктестингу, шаг 12).

### Шаг 7. Базовая модель прогноза

Фактический результат:

- Модуль `src/fantasy_analytics/forecast.py` строит прогноз ожидаемых
  fantasy-очков из leakage-free feature-датасета шага 6 (вызывает
  `build_feature_dataset`, поэтому наследует cutoff и защиту от утечек). Модель
  интерпретируемая и начинается с ожидаемых минут: очки за появление считаются
  из `p_appearance`/`expected_minutes` и доли полных матчей.
- Голы команд оцениваются Poisson-моделью: ожидаемые голы за/против —
  смесь венью-силы атаки/защиты (`0.5*(club_attack+opponent_defense)` и зеркало),
  а вероятность сухого матча = Poisson-вероятность, что соперник не забьёт
  (`exp(-λ)`). Вклад голов, ассистов, сейвов, возвратов и карточек считается из
  per-90 рейтингов игрока и ожидаемых минут.
- События конвертируются в очки версионированной таблицей `SCORING`
  (`scoring_version = rpl-2025-2026.1`), восстановленной из авторитетных
  матчевых `points` сезона (Sports.ru публикует правила только картинкой, а
  `statDetails` пуст). `expected_points` — точная сумма компонентов
  (`components` JSONB), плюс оценка неопределённости из независимых дисперсий
  компонентов.
- Помимо событийной модели (`poisson_events`) считаются два простых baseline:
  `season_mean` (средние очки за появление × вероятность выхода) и `recent_form`
  (среднее за последние 5 появлений × вероятность выхода).
- Таблица `player_forecasts` (модель `PlayerForecast`, миграция `0004`,
  autogenerate) хранит по строке на (snapshot-run, тур, матч, игрок, модель+
  версия) с `expected_points`, `uncertainty`, `p_appearance`,
  `expected_minutes`, `components`, `params`, `feature_version`,
  `scoring_version`. Уникальный констрейнт делает запись идемпотентной.
- `ForecastRepository` (`db/forecast_repository.py`) идемпотентно замещает
  прогнозы run/тур/модель (delete+insert). CLI `fantasy-forecast`
  (`forecast_cli.py`) строит, сохраняет и пишет `forecast.json`/`forecast.csv`;
  флаг `--no-persist` отключает запись в БД.

Критерии приёмки (выполнено):

- Прогноз строится для всех игроков тура — на боевом туре 1786 покрыты 590
  игроков (569 доступных), по 3 модели = 1770 строк.
- Сумма компонентов объясняет итог — по построению `expected_points =
  sum(components)`; проверено на всех 590 событийных строках (0 расхождений) и
  тестом `test_components_sum_to_expected_points`.
- Повторный расчёт детерминирован — идентичные отчёты при повторном прогоне
  (тест `test_recompute_is_deterministic` + боевая сверка двух прогонов).
- Есть простые baseline — `season_mean` и `recent_form`.
- Результаты сохраняются в БД с версией модели — `player_forecasts`
  (`model_name`/`model_version`/`feature_version`/`scoring_version`).

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/step7-baseline-forecast-model-bcb6`
- Commit/PR: PR #9
- Проверки:
  - `python -m unittest discover -s tests` — 106 тестов проходят (было 82),
    1 live-skip; добавлен `tests/test_forecast.py` (24 теста: Poisson-хелперы,
    смесь голов, разбиение появления, алгебра компонентов, baseline и
    интеграционные сценарии сумм/детерминизма/идемпотентной записи).
  - Миграции: цепочка `0003 → 0004` создаёт `player_forecasts`,
    `downgrade`/`upgrade` проходят, `compare_metadata(models, db)` = 0
    расхождений.
  - Вывод правил начисления: реконструкция 9578 матчевых строк по таблице
    `SCORING` совпадает точно в 83% и в пределах ±1 очка в 96%; остаток —
    непрямой fantasy-ассист и поздние возвраты владения, отсутствующие в
    импортированных матчевых колонках.
  - Боевой e2e на активном snapshot (run 1, сезон 2025/2026), тур 1786:
    `fantasy-forecast --tour 1786` построил и сохранил 1770 прогнозов; топ
    событийной модели (Батраков 7.85, Сперцян 7.62, Глушенков 7.10) совпадает с
    реальными лидерами; повторный прогон оставил 1770 строк без дублей
    (идемпотентность), отчёты идентичны (детерминизм).
- Решения и отклонения:
  - Шаг 6 расширен минимально: `features.py` добавляет per-90 сейвов, возвратов
    и жёлтых (нужны событийной модели), `FEATURE_VERSION` → `1.1.0`. Это
    сохраняет единый leakage-контролируемый путь данных вместо дублирующей
    загрузки в шаге 7.
  - Таблица начисления не хранится структурно в API (только `rules_html` с
    картинкой, `statDetails` пуст), поэтому `SCORING` восстановлена из
    авторитетных матчевых `points` и версионирована; корректность подтверждена
    реконструкцией.
  - Голы игрока оцениваются снизу-вверх по per-90 рейтингам, а Poisson-модель
    голов команды используется для вероятности сухого матча (сохраняет
    аддитивность и интерпретируемость компонентов).
  - Неопределённость — стандартное отклонение из независимых дисперсий
    компонентов (Poisson для счётных событий, Бернулли для сухого матча и полной
    игры); это документированная аппроксимация, а не калиброванный интервал.

### Шаг 8. Оптимизатор состава

Фактический результат:

- Модуль `src/fantasy_analytics/optimizer.py` — целочисленная модель OR-Tools
  CP-SAT. Переменные `pick` (в составе из 15), `start` (в старте из 11) и
  `captain` с ограничениями `start ≤ pick`, `captain ≤ start` и ровно один
  капитан. Objective максимизирует ожидаемые очки стартового состава плюс
  капитан (удваивается), со строгим вторичным tie-break, минимизирующим расход
  бюджета (максимум свободных средств). Вице-капитан — лучший из оставшихся
  стартовых, скамейка упорядочена по ожидаемым очкам, запасной вратарь — в конце.
- Бюджет и позиционные лимиты (`full_roster_constraints`/
  `starting_roster_constraints`) берутся из `season_rules`; клубный лимит
  (`max_same_team_players`) и лимит трансферов (`total_transfers`) — из целевого
  `fantasy_tours`. Ничего не захардкожено (`parse_role_limits` разбирает список
  `{role, minCount, maxCount}`).
- Два режима: `squad` (новый состав) и `transfers` (сохраняется текущий состав,
  меняется не более лимита тура; `--max-transfers` переопределяет). Игроки,
  отсутствующие в пуле кандидатов, помечаются как вынужденные трансферы.
- Независимый `validate_squad` перепроверяет все правила по готовому решению без
  доверия solver'у; неразрешимая задача даёт понятный `OptimizerError`.
- Детерминизм: один search-worker, фиксированный seed, детерминированный
  порядок кандидатов и tie-break по расходу.
- CLI `fantasy-optimize` (`optimizer_cli.py`) строит состав из активного
  snapshot, пишет `optimizer.json` и печатает человекочитаемую сводку. Прогноз
  пересобирается in-process из активного snapshot (или `--run-id`), поэтому
  результат воспроизводим из `(run_id, tour, model, optimizer)`.

Критерии приёмки (выполнено):

- Каждый результат проходит отдельный validator — `validate_squad`
  (unit-тесты подтверждают отлов подделанных бюджета/размера/капитана/клуба).
- Fixtures для обычного тура и тура с изменённым лимитом трансферов —
  `SolveSquadTest` (обычный) и `TransfersModeTest` (лимиты 1 и 3);
  боевой прогон тура 1786 при лимите 1 → 63.65, при лимите 3 → 76.30.
- Solver сообщает понятную ошибку для неразрешимой задачи — `OptimizerError`
  (пул мал, бюджет мал, отрицательный лимит трансферов, нет клубного лимита).
- Objective соответствует сумме ожидаемых очков с учётом капитана — проверено
  `test_objective_equals_starting_plus_captain` и по построению.
- Результат детерминирован при одинаковых входах —
  `test_deterministic`/`test_end_to_end_deterministic`.

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/step8-squad-optimizer-d3bf`
- Commit/PR: PR #11
- Проверки:
  - `python -m unittest discover -s tests` — 134 теста проходят (было 106),
    1 live-skip; добавлен `tests/test_optimizer.py` (28 тестов: разбор лимитов,
    сборка кандидатов, solver обычного тура, режим трансферов с разными лимитами,
    infeasible, детерминизм, validator и e2e через БД).
  - Боевой e2e на активном snapshot (run 1, сезон 2025/2026), тур 1786:
    `fantasy-optimize --tour 1786` → OPTIMAL, схема 4-5-1, бюджет 100.0/100.0,
    ожидаемые очки с капитаном 76.30; капитан Батраков (7.85), вице Сперцян
    (7.62); состав 2 GK / 5 DEF / 5 MID / 3 FWD, ≤3 из клуба.
  - Режим трансферов: из заведомо слабого состава (звёзды заменены дешёвыми) при
    лимите 3 → 3/3 трансфера, звёзды возвращены, EP 76.30; при лимите 1 →
    1/1 трансфер, EP 63.65. Оба состава прошли независимый validator.
- Решения и отклонения:
  - Добавлена зависимость `ortools` (CP-SAT), как предписано планом; установка
    через `pip --break-system-packages`, добавлена в `pyproject.toml`.
  - Оптимизатор не добавляет таблиц: результат воспроизводим из forecast-пути и
    пишется артефактом на диск (аналогично шагу 6). Персист состава в БД — при
    необходимости в шаге 9 (REST API).
  - Tie-break минимизирует расход среди решений с равными очками
    (максимизирует свободный бюджет) и делает решение уникальным; на боевом туре
    оптимум расходует весь бюджет, так как дорогие полузащитники дают больше очков.

### Шаг 9. Пользовательский REST API

Фактический результат:

- Read-слой `src/fantasy_analytics/read_repository.py` (`ReadRepository`) читает
  каталог, активный snapshot и persisted-прогнозы одним набором запросов и
  возвращает JSON-совместимые dict'ы; ничего не пишет и не вызывает Sports.ru.
- Контракты `src/fantasy_analytics/api_schemas.py`: Pydantic-модели запросов и
  ответов, конверты пагинации (`PageMeta`), snapshot-метаданные (`SnapshotMeta`)
  и единый формат ошибок (`ErrorResponse`). Верхний лимит страницы — 200.
- `src/fantasy_analytics/api.py` расширен (приложение `Fantasy Analytics API`
  `0.2.0`): read-эндпоинты `GET /seasons[/{id}]`, `/tours[/{id}]`,
  `/matches[/{id}]`, `/players[/{id}]`, оптимизатор `POST /optimizer/squad` и
  `/optimizer/transfers`; сохранены админ-эндпоинты шага 5. Добавлены
  обработчики `StarletteHTTPException` и `RequestValidationError`, приводящие
  все ошибки к `{"error": {type, message, details}}`.
- Фильтры игроков (позиция, клуб, статус, диапазон цены), сортировка и
  подключение projection+components по `tour_id`+`model`. В списках — время
  snapshot (`data_freshness`), в projection — версии модели/признаков/начисления.
- Экспорт OpenAPI без БД: `src/fantasy_analytics/openapi_cli.py`
  (`fantasy-openapi`); схема зафиксирована в `docs/openapi.json`.

Критерии приёмки (выполнено):

- API покрыт unit- и integration-тестами — `tests/test_read_api.py` (26 тестов):
  контракты запросов, конверт ошибок, оптимизатор (успех через мок и реальный
  infeasible → `422`), OpenAPI, плюс интеграционные проверки всех read-эндпоинтов
  на реальной БД (импорт+публикация через worker, персист прогнозов).
- OpenAPI отражает реальные модели — `docs/openapi.json` содержит все 13 путей и
  компоненты (`PlayerListResponse`, `TransfersRequest`, …), генерируется из
  живого приложения.
- Пагинация стабильна и ограничена сверху — `limit ≤ 200`, `offset ≥ 0`,
  детерминированный порядок с tie-break по `player_season_id` (тест
  `test_players_pagination_is_bounded_and_stable`).
- В ответах есть время snapshot и версия модели — `snapshot.data_freshness` в
  списках/деталях, `model_version`/`feature_version`/`scoring_version` в
  projection.
- GraphQL Sports.ru не вызывается из read-эндпоинтов — весь read-слой работает
  только через SQLAlchemy-запросы к PostgreSQL.

Карточка выполнения:

- Начат: 2026-07-21
- Завершён: 2026-07-21
- Агент/ветка: `cursor/step9-user-rest-api-6b12`
- Commit/PR: PR #14
- Проверки:
  - `python -m unittest discover -s tests` — 160 тестов проходят (было 134),
    1 live-skip; добавлен `tests/test_read_api.py` (26), обновлён конверт `409`
    в `tests/test_api.py`.
  - `python -m compileall src` — синтаксис чист; OpenAPI генерируется офлайн
    (`fantasy-openapi`, 13 путей).
  - Боевой e2e на активном snapshot (run 1, сезон 2025/2026), тур 1786
    (прогнозы персистированы `fantasy-forecast --tour 1786`, 590×3):
    `GET /seasons` вернул сезон со `snapshot.data_freshness`;
    `GET /players?role=MIDFIELDER&order=projection` — топ Батраков 7.85,
    Сперцян 7.62, Глушенков 7.10 с компонентами, `total=244`, snapshot в ответе;
    `GET /players/2` — карточка с 28 матчами истории и projection;
    `POST /optimizer/squad {"tour":"1786"}` → OPTIMAL 4-5-1, EP 76.30,
    капитан Батраков (совпадает с шагом 8);
    `POST /optimizer/transfers` (max_transfers=2, уже оптимальный состав) →
    0/2 трансфера, kept 15, EP 76.30; `GET /seasons/999` → `404`
    `{"error":{"type":"not_found",…}}`; `limit=500` → `422` `validation_error`.
- Решения и отклонения:
  - Projections read-эндпоинтов берутся из персистентной `player_forecasts`
    (а не пересчитываются на каждый запрос): read-путь остаётся дешёвым,
    чисто-DB и отражает версионированную модель; для наполнения тура нужен
    предварительный `fantasy-forecast --tour <id>`.
  - Оптимизатор в API переиспользует `build_squad_optimization` (шаг 8), который
    читает только БД через forecast-builder, поэтому «GraphQL не вызывается»
    выполняется и для optimizer-эндпоинтов.
  - Read-эндпоинты принимают внутренние id (`season_id`, `tour_id`) для
    детерминизма; оптимизатор — «мягкие» ссылки (`season`/`tour` как fantasy
    id/имя), как у CLI шага 8.
  - Конверт ошибки `409` админ-refresh приведён к общему формату
    (`error.details.job`), тест шага 5 обновлён соответствующе. Схема БД и
    миграции не менялись.

### Шаг 10. Аналитический frontend

Карточка выполнения:

- Начат: 2026-07-21
- Агент/ветка: `cursor/step10-analytical-frontend-b3bb`
- Статус: `IN_PROGRESS`

## Журнал обновлений

| Дата | Шаг | Изменение статуса | Commit/PR | Результат |
| --- | --- | --- | --- | --- |
| 2026-07-19 | 0 | `IN_PROGRESS → DONE` | `ac9be65` | Подтверждён API, добавлены прототип, DDL и Docker |
| 2026-07-19 | План | создан | — | Сформированы независимые шаги 1–13 |
| 2026-07-20 | 1 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/persistence-layer-migrations-bd72` |
| 2026-07-20 | 1 | `IN_PROGRESS → DONE` | `0b9d2d2` | SQLAlchemy 2, Alembic-миграции, repository для ingestion_runs/raw_api_responses |
| 2026-07-20 | 2 | `PLANNED → READY` | — | Разблокирован завершением шага 1 |
| 2026-07-21 | 2 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/full-historical-import-d9c2` |
| 2026-07-21 | 2 | `IN_PROGRESS → DONE` | PR #4 | Полный импорт сезона 2025/2026 (16/30/240, 590 игроков, 9578 match-stats), идемпотентно и атомарно |
| 2026-07-21 | Протокол | добавлен пререквизит | — | БД пуста при старте клауд-агента: перед шагами с данными обязателен импорт (шаг 2) |
| 2026-07-21 | 3 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step3-match-stats-spike-e44d` |
| 2026-07-21 | 3 | `IN_PROGRESS → DONE` | ветка `cursor/step3-match-stats-spike-e44d` | Разведка statMatch: CLI `fantasy-match-stats`, таблица покрытия 113 полей, контрактные тесты |
| 2026-07-21 | 4 | `PLANNED → READY` | — | Разблокирован завершением шагов 2 и 3 |
| 2026-07-21 | 4 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step4-data-quality-reconciliation-a55e` |
| 2026-07-21 | 4 | `IN_PROGRESS → DONE` | PR по ветке `cursor/step4-data-quality-reconciliation-a55e` | Gate качества `fantasy-quality`, таблица `data_quality_issues`, активный snapshot, reconciliation с окном 72ч |
| 2026-07-21 | Миграции | `0001` статичный baseline | — | Переписана начальная миграция под инкрементальные изменения (autogenerate), добавлена `0002` |
| 2026-07-21 | 5 | `PLANNED → READY` | — | Разблокирован завершением шагов 2 и 4 |
| 2026-07-21 | 5 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step5-manual-ingestion-job-7eae` |
| 2026-07-21 | 5 | `IN_PROGRESS → DONE` | PR #7 | FastAPI `fantasy-api`, таблица `ingestion_jobs`, worker-процесс с advisory lock; refresh `202`/`409`, статус переживает рестарт, e2e-импорт опубликован |
| 2026-07-21 | 6 | `PLANNED → READY` | — | Разблокирован завершением шага 4 |
| 2026-07-21 | 6 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step6-analytical-features-5fc2` |
| 2026-07-21 | 6 | `IN_PROGRESS → DONE` | PR по ветке `cursor/step6-analytical-features-5fc2` | CLI `fantasy-features`, leakage-free dataset (rolling 3/5/10, per-90, сила клубов/соперника, p_appearance/expected_minutes), словарь признаков |
| 2026-07-21 | 7 | `PLANNED → READY` | — | Разблокирован завершением шага 6 |
| 2026-07-21 | 7 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step7-baseline-forecast-model-bcb6` |
| 2026-07-21 | 7 | `IN_PROGRESS → DONE` | PR #9 | CLI `fantasy-forecast`, таблица `player_forecasts`, событийная модель (Poisson + восстановленная таблица начисления) и baseline `season_mean`/`recent_form`; аддитивные компоненты, детерминизм, идемпотентная запись |
| 2026-07-21 | 8 | `PLANNED → READY` | — | Разблокирован завершением шага 7 |
| 2026-07-21 | 8 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step8-squad-optimizer-d3bf` |
| 2026-07-21 | 8 | `IN_PROGRESS → DONE` | PR #11 | Оптимизатор `fantasy-optimize` на OR-Tools CP-SAT: 15 игроков, старт, капитан/вице, скамейка; лимиты из `season_rules`/`fantasy_tours`, режимы squad/transfers, независимый validator, детерминизм |
| 2026-07-21 | 9 | `PLANNED → READY` | — | Разблокирован завершением шагов 5, 7 и 8 |
| 2026-07-21 | 9 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step9-user-rest-api-6b12` |
| 2026-07-21 | 9 | `IN_PROGRESS → DONE` | PR #14 | Read API (сезоны/туры/матчи/игроки с фильтрами, projections+компоненты), `POST /optimizer/squad` и `/optimizer/transfers`, Pydantic-контракты, пагинация, единый формат ошибок, OpenAPI `docs/openapi.json`; read-путь только из БД |
| 2026-07-21 | 10 | `PLANNED → READY` | — | Разблокирован завершением шага 9 |
| 2026-07-21 | 10 | `READY → IN_PROGRESS` | — | Закреплён за `cursor/step10-analytical-frontend-b3bb` |
