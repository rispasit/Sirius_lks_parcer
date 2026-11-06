# Парсер расписания ЛК Sirius University

Достаёт расписание занятий из личного кабинета
`lks.siriusuniversity.ru` на текущую и следующую неделю и отдаёт
в виде JSON в stdout.

## Что делает

1. Авторизуется в ЛК: берёт cookie с диска, если они недействительны —
   логинится через форму с решением Yandex SmartCaptcha (RuCaptcha).
2. Запрашивает расписание через ICS-экспорт
   (`/schedule/export/students?from=...&to=...`).
   Если ICS недоступен — фолбэк на Livewire-компонент
   (`wire:initial-data` в HTML).
3. Печатает JSON в stdout, логи — в stderr.

## Требования

- Python 3.11+ (используется `ZoneInfo`, `str | None`).
- Аккаунт в ЛК Sirius University.
- Аккаунт на [RuCaptcha](https://rucaptcha.com).

## Установка

```bash
git clone <repo>
cd parcer_lks
python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## Настройка

1. Скопируй шаблон:
   ```bash
   cp .env.example .env
   ```

2. Заполни `.env`:

   - `SITE_USER`, `SITE_PASS` — логин и пароль от ЛК.
   - `RU_API_KEY` — ключ из [кабинета RuCaptcha](https://rucaptcha.com/setting).
   - `PROBE_URL` — любая страница ЛК, которая без авторизации редиректит
     на `/login`. По умолчанию `/profile`.
   - `LOGIN_URL`, `LOGIN_POST_URL` — обычно оба `/login`, но проверь
     в DevTools (Network → POST при нажатии «Войти» → Request URL).

## Запуск

```bash
python main.py > schedule.json
```

- **stdout** — JSON (чистый, без логов).
- **stderr** — лог (INFO/WARNING), виден в консоли.

Если нужны и JSON, и лог в файлах:

```bash
python main.py > schedule.json 2> schedule.log
```

Первый запуск: нет `cookies.json` → полный логин через капчу (~30–60 сек).
Второй запуск: cookie жива → мгновенный ответ (~0.5 сек).
Повторный логин — когда cookie протухнет (обычно раз в ~30 дней).

## Формат вывода

```json
{
  "schema_version": 1,
  "generated_at": 1700000000,
  "tz": "Europe/Moscow",
  "current": {
    "week_start": "2026-01-05",
    "week_end": "2026-01-11",
    "week_start_ts": 1767556800,
    "week_end_ts": 1768161599,
    "source": "ics",
    "events": [
      {
        "uid": "00000000-0000-0000-0000-000000000000",
        "start": "2026-01-05T10:20+03:00",
        "end": "2026-01-05T11:40+03:00",
        "start_ts": 1767597600,
        "end_ts": 1767602400,
        "all_day": false,
        "duration_min": 80,
        "subject": "Пример дисциплины",
        "type": "Практические занятия",
        "room": "Аудитория 1.1",
        "teachers": "Иванов Иван Иванович",
        "group": "ГРУППА-00/0"
      }
    ]
  },
  "next": { "...": "..." }
}
```

### Поля события

| Поле | Тип | Описание |
|---|---|---|
| `uid` | `str` | Стабильный ID (из ICS `UID` или хэш для Livewire) |
| `start` / `end` | `str` | ISO 8601 с поясом (`+03:00`). Секунды — только если != 0 |
| `start_ts` / `end_ts` | `int` | Unix time (секунды) |
| `all_day` | `bool` | Событие без времени |
| `duration_min` | `int` | Длительность в минутах |
| `subject` | `str \| null` | Название дисциплины |
| `type` | `str \| null` | Тип пары (из скобок `SUMMARY`) |
| `room` | `str \| null` | Аудитория |
| `teachers` | `str \| null` | Преподаватели через `;` |
| `group` | `str \| null` | Группа |

### Поля недели

| Поле | Описание |
|---|---|
| `week_start` / `week_end` | Границы недели (ISO-дата) |
| `week_start_ts` / `week_end_ts` | Те же границы в Unix time |
| `source` | `"ics"` или `"livewire"` — откуда данные |
| `events` | Список событий, отсортирован по `(start, uid)` |

### Метаданные

| Поле | Описание |
|---|---|
| `schema_version` | Версия схемы, сейчас `1` |
| `generated_at` | Момент генерации, Unix time |
| `tz` | Пояс, в котором времена в `start`/`end` — `Europe/Moscow` |

## Exit codes

| Код | Что значит |
|---|---|
| `0` | Успех, JSON в stdout |
| `1` | `AuthError` — общая ошибка авторизации |
| `2` | `.env` не заполнен |
| `3` | Битый `cookies.json` |
| `4` | Не разобрали HTML-форму логина |
| `5` | Ошибка RuCaptcha (баланс, ключ, капча не решается, сеть) |
| `6` | Сервер не пустил после POST (неверный пароль / капча) |
| `7` | Парсер не смог получить расписание (и ICS, и Livewire упали) |

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```

С покрытием:

```bash
pytest --cov=parser --cov=auth --cov-report=term-missing
```

Тесты покрывают `parser.py` (все публичные функции + helpers).
`auth.py` не покрыт — требует моков RuCaptcha и всего login-flow.

## Как это устроено

```text
main.py
  ├─ auth.get_session()       # cookie с диска или логин + капча
  └─ parser.get_week() × 2    # current + next
        ├─ _fetch_ics()       # основной источник: /schedule/export/students
        └─ _fetch_livewire()  # fallback: wire:initial-data в HTML
```

`auth.py` — «чёрный ящик»: публичный `get_session()`, всё остальное
(`_login`, `_solve_smartcaptcha`, `_is_valid`) — внутренности.

`parser.py` — два класса (`ScheduleEvent`, `ScheduleWeek`) и набор
приватных функций. Внешний код работает с ними через `get_week()`
и `get_schedule()`.

## Безопасность

- `.env` и `cookies.json` **никогда** не коммитятся — они в `.gitignore`.
- `cookies.json` пишется с правами `0600` (Linux/macOS).
- API-ключ RuCaptcha и пароль от ЛК — в `.env`, в коде их нет.
- Если у тебя ноутбук без шифрования диска — **это дыра**, а не cookie.
  FileVault / BitLocker / LUKS важнее любой возни с файлами.

## Известные ограничения

- **Livewire-фолбэк видит только текущий месяц.** ICS через границу
  месяца работает нормально (`?from=&to=` фильтрует сервер).
  Если ICS упадёт, «следующая неделя» на стыке месяцев из Livewire
  будет неполной — смотри поле `source` в JSON.
- **Только `Europe/Moscow`.** Незнакомый `TZID` трактуется как MSK,
  в лог пишется warning один раз на уникальный TZID.
- **Один аккаунт.** Если зайти в ЛК через браузер, `cookies.json`
  может протухнуть (если сервер разрешает только одну сессию).
  Тогда следующий запуск `main.py` сделает новый логин.
- **Схема меняется редко.** При несовместимых изменениях
  `schema_version` увеличивается — downstream должен проверять.
- **`schedule.json` не создаётся автоматически.** Если запускаешь
  `python main.py > schedule.json`, файл появится в текущей директории.
  Он в `.gitignore` и не попадёт в коммит.

## Лицензия

Этот проект распространяется под **GNU General Public License v3.0**.
Полный текст — в файле [LICENSE](LICENSE).

Copyright (C) 2026 Rispas
