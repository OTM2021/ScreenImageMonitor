from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

try:
    import mss as _mss  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("mss")
    stub.mss = lambda: None
    sys.modules["mss"] = stub

import screen_image_monitor_gui as gui


def main() -> int:
    default = gui.CounterDisplaySettings()
    assert default.font_family == "Segoe UI Light"
    assert default.font_size == 56
    assert default.font_weight == "normal"
    assert gui.COUNTER_BACKGROUND == "#3F80EC"
    assert gui.COUNTER_FOREGROUND == "#FFFFFF"

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "counter_display.json"
        loaded = gui.load_counter_display_settings(path)
        assert loaded == default

        custom = gui.CounterDisplaySettings(
            font_family="Arial",
            font_size=72,
            font_weight="bold",
        )
        gui.save_counter_display_settings(custom, path)
        assert gui.load_counter_display_settings(path) == custom

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["font_family"] == "Arial"
        assert raw["font_size"] == 72
        assert raw["font_weight"] == "bold"

        path.write_text(
            json.dumps(
                {
                    "font_family": "",
                    "font_size": 999,
                    "font_weight": "invalid",
                }
            ),
            encoding="utf-8",
        )
        sanitized = gui.load_counter_display_settings(path)
        assert sanitized.font_family == default.font_family
        assert sanitized.font_size == 180
        assert sanitized.font_weight == default.font_weight

    source = Path("screen_image_monitor_gui.py").read_text(encoding="utf-8")
    assert "フォント設定" in source
    assert "Segoe UI Light" in source
    assert "COUNTER_BACKGROUND" in source
    assert "counter_display.json" in source

    print("Counter font settings smoke test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
