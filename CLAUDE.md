# Limits Widget — контекст для Claude Code

Плавающий always-on-top виджет (Windows, Python + tkinter), который показывает лимиты Claude
(5 часов, неделя, расход, доллары, разбивка недели по продуктам). Нужен потому, что моды Claude Code
работают только во вкладке «Код»/в терминале, а во вкладке «Чат» десктопного приложения индикатора нет.

Владелец: Andrii (QA, говорит по-русски). **Отвечай и пиши интерфейсные тексты по-русски.**
Рабочая папка: `D:\Claude\limits-widget`. Запуск: `python limits_widget.py` (или `run.bat` — без консоли).

## Файлы

| Файл | Назначение |
|---|---|
| `limits_core.py` | Вся логика без GUI: парсинг, форматирование, цвета, чтение токена, `fetch_live`, `refresh_login`. Только stdlib. |
| `limits_widget.py` | tkinter-виджет (класс `Widget`) + хелперы мониторов, конфиг, DPI, темы. |
| `limits_statusline.py` | Скрипт для `statusLine` Claude Code: читает JSON со stdin, атомарно пишет `~/.claude/limits-snapshot.json`, печатает короткую строку. |
| `test_core.py`, `test_widget_smoke.py` | unittest. Виджетный смоук-тест подменяет `tkinter` заглушкой (Dummy/Var), поэтому работает без дисплея. |
| `README.md` | Инструкция для пользователя. |

Тесты: `python -m unittest -q` (сейчас 55, все проходят). Запускать после любой правки.

## Источники данных (режимы `mode`)

- **file** (по умолчанию, безопасный): читает `~/.claude/limits-snapshot.json`, который пишет statusLine-скрипт
  во время сессий Claude Code. Опрос раз в 5 с.
- **live**: `GET https://api.anthropic.com/api/oauth/usage` с токеном из `~/.claude/.credentials.json`
  (`claudeAiOauth.accessToken`, `expiresAt` в мс), заголовок `anthropic-beta: oauth-2025-04-20`.
  Неофициальный эндпоинт (серая зона по ToS), поэтому включается только после диалога согласия (`live_ok`).
  Опрос раз в 120 с, при 429 — backoff. 401/403 или истёкший токен → `Data.auth_error = True`.
- Поля ответа: `utilization` (0–100) и `resets_at` (ISO) у окон; `limits[]` с `severity`;
  `seven_day_breakdown.rows`; `extra_usage`, `spend`, `limit_dollars/used_dollars`.
  Неизвестные окна подхватываются автоматически (любой dict-ключ с `utilization`/`used_percentage`), но
  по умолчанию скрыты; известные: `five_hour`, `seven_day`, `seven_day_opus/sonnet`, `spend_limit`, `iguana_necktie`
  («Лимит в $», значение пока непонятно — оставлено скрытым).
- На тарифе Pro per-model окна приходят `null` → доли по моделям можно получить только из локальных логов
  `~/.claude/projects/*.jsonl` (только Claude Code, это этап 4).

## Обновление входа

Кнопка «Обновить вход» на виджете и авто-обновление (`auto_login`): `refresh_login()` по шагам запускает
`claude auth status`, затем крошечный `claude -p "ok" --max-turns 1 --model haiku --no-session-persistence`
и каждый раз перечитывает credentials (`token_ok`). Авто: кулдаун 30 мин (`AUTO_LOGIN_COOLDOWN`), максимум
3 подряд неудачи (`AUTO_LOGIN_MAX_FAILS`). Процесс запускается с `CREATE_NO_WINDOW`, cwd — temp, stdin — DEVNULL.

## Устройство виджета

- Конфиг: `~/.claude-limits-widget.json`, значения по умолчанию — `DEFAULTS` в `limits_widget.py`
  (`view` bars/rings, `size` S/M/L, `theme` auto/dark/light, `accent`, `compact`, `pulse`, `snap`, `ghost`,
  `reset_abs`, `rounded`, `auto_login`, `pace`, `history`, `notify`, `hidden`, `shown_extra`, `show_breakdown`, `alpha`, `topmost`, `x`, `y`).
  Новый ключ добавлять в `DEFAULTS`, валидацию — в `load_config`.
- Отрисовка: один `Canvas`, `render()` вызывает `_draw_header / _draw_bars | _draw_rings | _draw_compact /
  _draw_breakdown / _draw_context / _draw_note / _draw_login_button`. Кнопки на канвасе — через `_buttons`
  и `_hit_button`.
- Всё в пикселях умножается через `self.px(n)` и шрифты через `self.font(pt)` (масштаб size + DPI).
- Окно: `overrideredirect`, `-topmost`, `-alpha`, DPI awareness через ctypes (`SetProcessDpiAwareness(2)`),
  скругление — experimental через `-transparentcolor "#010203"`.
- Несколько мониторов: `monitor_rects()` (ctypes `EnumDisplayMonitors`/`GetMonitorInfoW`, рабочие области),
  `pick_monitor`, `snap_position`, `clamp_into`. Прилипание и ограничение позиции считаются по монитору, на котором
  центр окна; отрицательные координаты (монитор слева) работают, геометрия задаётся как `"+-1920+0"`.
  Если монитор отключён — окно уезжает на ближайший.
- Цвета шкал: `color_for(percent, severity)` — зелёный → янтарный → красный, пульсация при ≥90%.

## Правила разработки

1. Логику держать в `limits_core.py` без tkinter; GUI-код — в `limits_widget.py`. Новая логика → тест.
2. Не ломать смоук-тест: заглушка `tkinter` знает только то, что реально используется; новые виджеты/методы
   tkinter добавлять и в заглушку.
3. Любой сетевой/подпроцессный вызов — с таймаутом, исключения не должны валить GUI (ошибка идёт в `Data.error`).
4. Не писать и не логировать токены. Токен читается только в `read_token`.
5. Тексты интерфейса — по-русски, короткие.

## Сделано

Этап 1: все окна лимитов, фильтр показа, неделя по продуктам, «Сырой ответ…», тёмная/светлая/авто тема, акценты,
размеры S/M/L, режимы полосы/кольца, компактный режим (разворот по наведению), индикатор свежести,
пульсация ≥90%, прилипание к краям, «призрак», абсолютное время сброса (клик), скруглённые углы,
кнопка и авто-обновление входа, поддержка нескольких мониторов (прилипание + сохранение позиции).

Этап 2: метка ровного темпа (`pace_percent`, длины окон в `DURATIONS`), прогноз исчерпания по среднему темпу
с начала окна (`forecast_exhaust`, не раньше 5% окна), уведомления на 80/90% и о сбросе (`check_alerts`:
первое появление окна молча, сброс = падение процента >10 пунктов после порога; показ — `show_toast`,
`tk.Toplevel` + `tk.Label`), история `~/.claude-limits-history.jsonl` (`history_record`/`should_record`/
`append_history`/`read_history`, ротация в `.1` после 1 МБ, heartbeat 30 мин). Проверено на реальном tkinter
коротким скриптом (без ошибок в колбэках); вид на экране глазами ещё не смотрели.

## План (утверждён: «давай делать все, поэтапно»)

- **Этап 3.** Карточка сессии Claude Code: расширить `limits_statusline.py` (модель, стоимость, время, контекст,
  доля попаданий в кэш, подсказка про `/compact`, несколько сессий) и показать в виджете.
- **Этап 4.** Доли по моделям из `~/.claude/projects/*.jsonl` (только Code).
- **Этап 5.** Иконка в трее, мини-график истории.
- **Дизайн (ждёт выбора пользователя):** переключатель стиля шкал (сплошная / сегменты / градиент /
  полукруг-gauge / концентрические кольца), пресеты, анимации, пользовательские пороги, подсказки при наведении.

## Известные ограничения

- GUI не запускался автоматически ни разу: проверка на реальном экране (мультимонитор, DPI, прозрачность,
  скругление) — только вручную. В Claude Code запускай виджет и смотри traceback.
- Live-эндпоинт неофициальный и может измениться или отдавать 429.
- Какое именно действие CLI обновляет токен, документация не уточняет — поэтому шаги обновления перечитывают
  credentials после каждого.

## GitHub

Репозиторий: https://github.com/AndriiLievientsov/Claude-limits-widget (публичный, пока только README).
Работать прямо в клоне: `git clone` в `D:\Claude\limits-widget` или добавить `origin` к существующей папке.
Коммитить небольшими шагами (один этап — один-два коммита), перед коммитом гонять `python -m unittest -q`.
Не коммитить токены, `~/.claude/*`, `limits-snapshot.json` и личный конфиг `~/.claude-limits-widget.json`
(они и так вне папки проекта). В README и коде не должно быть личных путей: пример с `D:\Claude\...`
в README заменить на `<папка проекта>` перед публикацией.
