"""Строка состояния Claude Code: сохраняет лимиты в файл для виджета и печатает короткий текст.

Claude Code передаёт JSON на stdin (поля rate_limits и context_window).
Подключение описано в README.
"""
import json
import os
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
SNAPSHOT = CLAUDE_DIR / "limits-snapshot.json"


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    rate = data.get("rate_limits") or {}
    ctx = (data.get("context_window") or {}).get("used_percentage")

    # Пустые лимиты (до первого ответа) не затирают прошлый снимок.
    if rate or not SNAPSHOT.exists():
        try:
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SNAPSHOT.with_suffix(".tmp")
            tmp.write_text(json.dumps({"saved_at": time.time(), "rate_limits": rate,
                                       "context_percent": ctx}), encoding="utf-8")
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
