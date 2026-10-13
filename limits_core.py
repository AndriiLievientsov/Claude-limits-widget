"""Логика виджета лимитов Claude: чтение данных, без GUI. Только стандартная библиотека."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
SNAPSHOT = CLAUDE_DIR / "limits-snapshot.json"
CREDENTIALS = CLAUDE_DIR / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

LABELS = {
    "five_hour": "5 часов",
    "seven_day": "Неделя",
    "seven_day_opus": "Неделя · Opus",
    "seven_day_sonnet": "Неделя · Sonnet",
    "spend_limit": "Расход",
    "iguana_necktie": "Лимит в $",
}
SHORT = {"five_hour": "5 ч", "seven_day": "Нед", "seven_day_opus": "Opus", "seven_day_sonnet": "Sonnet",
         "spend_limit": "Расход", "iguana_necktie": "$"}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
KNOWN = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "spend_limit"]
SEVERITY_KIND = {"session": "five_hour", "weekly_all": "seven_day"}
BREAKDOWN_NAMES = {"claude_code": "Code", "chat": "Чаты", "cowork": "Cowork", "other": "Прочее"}
BREAKDOWN_COLORS = {"claude_code": "#3b82f6", "chat": "#a78bfa", "cowork": "#f59e0b", "other": "#8a8a8a"}
ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "spend_limit"]


def label_for(kind: str) -> str:
    """Подпись окна; для незнакомых ключей берём сам ключ (see «Сырой ответ»)."""
    return LABELS.get(kind) or kind.replace("_", " ").capitalize()


@dataclass
class Window:
    kind: str
    percent: float
    resets_at: float | None  # unix-секунды
    severity: str | None = None
    used_dollars: float | None = None
    limit_dollars: float | None = None


@dataclass
class Share:
    key: str
    name: str
    percent: float


@dataclass
class Data:
    windows: list[Window] = field(default_factory=list)
    context_percent: float | None = None
    updated_at: float | None = None  # когда данные получены
    error: str | None = None
    raw: dict | None = None  # ответ целиком (без токена), для «Сырого ответа»
    breakdown: list = field(default_factory=list)  # доли недели по продуктам (Share)
    auth_error: bool = False  # проблема со входом (токен истёк, нет файла): лечится кнопкой «Обновить вход»


def parse_reset(value):
    """Принимает unix-секунды/миллисекунды или ISO-строку, отдаёт unix-секунды."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def parse_windows(rate_limits: dict, now: float | None = None) -> list[Window]:
    """Достаёт окна лимитов из словаря. Понимает и used_percentage (Claude Code), и utilization (API).

    Берёт любой ключ, значение которого похоже на окно (словарь с процентом), известные идут первыми.
    """
    now = now if now is not None else time.time()
    rate_limits = rate_limits if isinstance(rate_limits, dict) else {}
    keys = [k for k in ORDER if k in rate_limits] + sorted(k for k in rate_limits if k not in ORDER)
    severity = {}
    for item in rate_limits.get("limits") or []:
        if isinstance(item, dict) and SEVERITY_KIND.get(item.get("kind")):
            severity[SEVERITY_KIND[item["kind"]]] = item.get("severity")
    out = []
    for kind in keys:
        raw = rate_limits.get(kind)
        if not isinstance(raw, dict):
            continue
        pct = raw.get("used_percentage")
        if pct is None:
            pct = raw.get("utilization")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        reset = parse_reset(raw.get("resets_at"))
        if reset is not None and reset < now:
            pct, reset = 0.0, None  # окно уже сбросилось
        used, limit = raw.get("used_dollars"), raw.get("limit_dollars")
        out.append(Window(kind, float(pct), reset, severity.get(kind),
                          float(used) if isinstance(used, (int, float)) else None,
                          float(limit) if isinstance(limit, (int, float)) else None))
    return out


def parse_breakdown(body) -> list:
    """Доли недельного расхода по продуктам (Code, Чаты, Cowork, Прочее) из seven_day_breakdown."""
    rows = ((body or {}).get("seven_day_breakdown") or {}).get("rows") if isinstance(body, dict) else None
    out = []
    for r in rows or []:
        if isinstance(r, dict) and isinstance(r.get("percent"), (int, float)):
            key = str(r.get("key", ""))
            out.append(Share(key, BREAKDOWN_NAMES.get(key) or r.get("display_name") or key, float(r["percent"])))
    return out


def read_snapshot(now: float | None = None) -> Data:
    """Режим «файл»: данные, которые записал limits_statusline.py."""
    try:
        raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Data(error="Нет данных. Запусти claude с подключённой строкой состояния (см. README).")
    except (OSError, ValueError) as e:
        return Data(error=f"Не прочитать {SNAPSHOT.name}: {e}")
    windows = parse_windows(raw.get("rate_limits") or {}, now)
    if not windows:
        return Data(updated_at=raw.get("saved_at"), context_percent=raw.get("context_percent"),
                    error="В данных нет лимитов (нужна подписка Pro/Max и один ответ в Claude Code).")
    return Data(windows, raw.get("context_percent"), raw.get("saved_at"), raw=raw.get("rate_limits"))


def read_token():
    """Токен входа Claude Code. Возвращает (token, error)."""
    try:
        raw = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "Нет файла входа. Выполни вход: запусти claude."
    except (OSError, ValueError) as e:
        return None, f"Не прочитать файл входа: {e}"
    oauth = raw.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None, "В файле входа нет токена. Выполни вход: запусти claude."
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        return None, "Токен истёк. Запусти claude, он обновит его."
    return token, None


def fetch_live(timeout: float = 15) -> Data:
    """Режим «live»: неофициальный адрес Anthropic с токеном Claude Code."""
    token, err = read_token()
    if err:
        return Data(error=err, auth_error=True)
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "claude-limits-widget/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return Data(error="429: слишком частые запросы, подожди")
        if e.code in (401, 403):
            return Data(error=f"{e.code}: токен не принят. Запусти claude, чтобы обновить вход.", auth_error=True)
        return Data(error=f"Ошибка сервера {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Data(error=f"Нет связи: {getattr(e, 'reason', e)}")
    except ValueError:
        return Data(error="Ответ не похож на JSON: формат мог измениться")
    windows = parse_windows(body)
    if not windows:
        return Data(error="В ответе нет лимитов: формат мог измениться")
    return Data(windows, None, time.time(), raw=body if isinstance(body, dict) else None,
                breakdown=parse_breakdown(body))


def short_label(kind: str) -> str:
    return SHORT.get(kind) or label_for(kind)


def format_reset_abs(resets_at, now: float | None = None) -> str:
    """Точное время сброса: «в 14:40» в пределах суток, дальше «вс 18:00»."""
    if resets_at is None:
        return ""
    now = now if now is not None else time.time()
    if resets_at <= now:
        return "сброс"
    tm = time.localtime(resets_at)
    if resets_at - now < 20 * 3600:
        return time.strftime("в %H:%M", tm)
    return f"{WEEKDAYS[tm.tm_wday]} {time.strftime('%H:%M', tm)}"


def mix(c1: str, c2: str, t: float) -> str:
    """Смешивает два цвета #rrggbb: t=0 даёт c1, t=1 даёт c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def format_reset(resets_at, now: float | None = None) -> str:
    if resets_at is None:
        return ""
    now = now if now is not None else time.time()
    secs = int(resets_at - now)
    if secs <= 0:
        return "сброс"
    mins = secs // 60
    if mins < 60:
        return f"{mins} мин"
    hours, m = divmod(mins, 60)
    if hours < 48:
        return f"{hours} ч {m:02d} мин"
    return f"{round(hours / 24)} дн"


# ---------- темп и прогноз ----------
DURATIONS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400, "seven_day_opus": 7 * 86400,
             "seven_day_sonnet": 7 * 86400}
MIN_ELAPSED_SHARE = 0.05  # раньше 5% окна прогноз слишком шумный


def window_elapsed(w: Window, now: float | None = None):
    """(прошло секунд, длина окна) или None, если длина окна или время сброса неизвестны."""
    dur = DURATIONS.get(w.kind)
    if not dur or w.resets_at is None:
        return None
    now = now if now is not None else time.time()
    return min(max(dur - (w.resets_at - now), 0.0), float(dur)), dur


def pace_percent(w: Window, now: float | None = None):
    """Где была бы шкала при ровном расходе: доля прошедшего времени окна, 0–100."""
    el = window_elapsed(w, now)
    return None if el is None else el[0] / el[1] * 100


def forecast_exhaust(w: Window, now: float | None = None):
    """Когда лимит кончится при среднем темпе с начала окна (unix-секунды) или None, если хватит до сброса."""
    now = now if now is not None else time.time()
    el = window_elapsed(w, now)
    if el is None or w.percent <= 0:
        return None
    elapsed, dur = el
    if w.percent >= 100:
        return now
    if elapsed < dur * MIN_ELAPSED_SHARE:
        return None
    at = now + (100 - w.percent) * elapsed / w.percent
    return at if at < w.resets_at else None


GREEN = "#30a46c"


def color_for(percent: float, severity: str | None = None, normal: str = GREEN) -> str:
    """Цвет шкалы: по проценту, а если сервер назвал серьёзность не «normal», то по ней."""
    sev = (severity or "normal").lower()
    if sev != "normal":
        return "#e5484d" if any(w in sev for w in ("crit", "high", "exceed", "limit", "error", "block")) else "#f5a524"
    if percent >= 90:
        return "#e5484d"
    if percent >= 70:
        return "#f5a524"
    return normal


# ---------- обновление входа ----------
def find_claude() -> str | None:
    exe = shutil.which("claude") or shutil.which("claude.exe")
    if exe:
        return exe
    for name in ("claude.exe", "claude"):
        cand = Path.home() / ".local" / "bin" / name
        if cand.exists():
            return str(cand)
    return None


def token_ok() -> bool:
    return read_token()[1] is None


def run_claude(exe: str, args: list, timeout: float):
    """Запускает claude без окна консоли, из временной папки, без доступа к вводу."""
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                          cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL, **kw)


REFRESH_STEPS = [
    ["auth", "status"],  # бесплатно; может сам обновить токен
    ["-p", "ok", "--max-turns", "1", "--model", "haiku", "--no-session-persistence"],  # один крошечный запрос
]


def refresh_login(timeout: float = 90) -> tuple[bool, str]:
    """Просит Claude Code обновить токен входа. Возвращает (получилось, сообщение)."""
    if token_ok():
        return True, "Вход уже действителен"
    exe = find_claude()
    if not exe:
        return False, "Не найден claude. Установи Claude Code и войди: claude auth login"
    last = ""
    for args in REFRESH_STEPS:
        try:
            res = run_claude(exe, args, timeout)
        except subprocess.TimeoutExpired:
            return False, f"claude не ответил за {int(timeout)} с"
        except OSError as e:
            return False, f"Не запустить claude: {e}"
        if token_ok():
            return True, "Вход обновлён"
        out = (res.stderr or res.stdout or "").strip().splitlines()
        last = out[0][:80] if out else ""
    return False, "Токен не обновился" + (f": {last}" if last else "") + ". Войди вручную: claude auth login"
