"""Строка состояния Claude Code: сохраняет лимиты в файл для виджета и печатает короткий текст.

Claude Code передаёт JSON на stdin (поля rate_limits, context_window, model, cost, session_id).
Подключение описано в README.
"""
import json
import os
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
SNAPSHOT = CLAUDE_DIR / "limits-snapshot.json"
SESSION_TTL = 6 * 3600  # сессии старше этого из снимка выбрасываем
MAX_SESSIONS = 5


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_session(data: dict, now: float) -> dict | None:
    """Короткая карточка текущей сессии из JSON строки состояния (None, если нет session_id)."""
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    cost = data.get("cost") if isinstance(data.get("cost"), dict) else {}
    ctx = data.get("context_window") if isinstance(data.get("context_window"), dict) else {}
    usage = ctx.get("current_usage") if isinstance(ctx.get("current_usage"), dict) else {}
    ws = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    folder = ws.get("current_dir") or data.get("cwd") or ""
    return {
        "id": sid,
        "model": model.get("display_name") or model.get("id"),
        "cost_usd": _num(cost.get("total_cost_usd")),
        "duration_ms": _num(cost.get("total_duration_ms")),
        "context_percent": _num(ctx.get("used_percentage")),
        "cache_read": _num(usage.get("cache_read_input_tokens")),
        "cache_create": _num(usage.get("cache_creation_input_tokens")),
        "input_tokens": _num(usage.get("input_tokens")),
        "folder": Path(folder).name if folder else None,
        "updated_at": now,
    }


def merge_sessions(old, session: dict | None, now: float) -> dict:
    """Добавляет/обновляет сессию, выбрасывает старые, оставляет MAX_SESSIONS свежих."""
    sessions = {k: v for k, v in (old if isinstance(old, dict) else {}).items()
                if isinstance(v, dict) and now - (v.get("updated_at") or 0) < SESSION_TTL}
    if session:
        sessions[session["id"]] = session
    newest = sorted(sessions, key=lambda k: sessions[k].get("updated_at") or 0, reverse=True)[:MAX_SESSIONS]
    return {k: sessions[k] for k in newest}


def _read_old() -> dict:
    try:
        old = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        return old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        return {}


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    rate = data.get("rate_limits") or {}
    ctx = (data.get("context_window") or {}).get("used_percentage")
    now = time.time()

    old = _read_old()
    sessions = merge_sessions(old.get("sessions"), build_session(data, now), now)
    # Пустые лимиты (до первого ответа) не затирают прошлые, но сессии обновляем всегда.
    snap = {"saved_at": now, "rate_limits": rate, "context_percent": ctx, "sessions": sessions}
    if not rate and old.get("rate_limits"):
        snap["rate_limits"] = old["rate_limits"]
        snap["saved_at"] = old.get("saved_at", now)
        snap["context_percent"] = old.get("context_percent")
    try:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        os.replace(tmp, SNAPSHOT)
    except OSError:
        pass

    parts = []
    for key, label in (("five_hour", "5ч"), ("seven_day", "нед")):
        pct = (rate.get(key) or {}).get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append(f"{label} {pct:.0f}%")
    if isinstance(ctx, (int, float)):
        parts.append(f"контекст {ctx:.0f}%")
    sys.stdout.write(" · ".join(parts))


if __name__ == "__main__":
    main()
