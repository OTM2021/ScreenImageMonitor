from __future__ import annotations

import json
import tempfile
from pathlib import Path

import sys
import types

try:
    import mss as _mss  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("mss")
    stub.mss = lambda: None
    sys.modules["mss"] = stub

import screen_image_monitor as engine


def make_legacy_config() -> dict[str, object]:
    return {
        "coordinate_mode": "absolute",
        "check_interval_seconds": 0.5,
        "show_status": True,
        "count_file": "counts.json",
        "evidence_dir": "evidence",
        "rules": [
            {
                "name": "legacy-sound-rule",
                "detector": "number",
                "action": "sound",
                "sound": "sounds/alert.wav",
                "region": {"left": 0, "top": 0, "width": 100, "height": 40},
                "condition": {"operator": "ge", "value": 10},
                "ocr": {
                    "psm": 7,
                    "scale": 3.0,
                    "threshold": "otsu",
                    "invert": False,
                    "whitelist": "0123456789",
                    "timeout_seconds": 2.0,
                    "number_index": 0,
                    "border": 10,
                },
                "required_matches": 1,
                "save_evidence": False,
            }
        ],
    }


def main() -> int:
    original_config_path = engine.CONFIG_PATH
    original_save_counts = engine.save_counts
    original_save_evidence = engine.save_evidence_image
    original_play_sound = engine.play_sound
    sound_calls: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(make_legacy_config()),
                encoding="utf-8",
            )
            engine.CONFIG_PATH = config_path
            config = engine.load_config()
            rule = config.rules[0]
            assert rule.action == "count"
            assert rule.sound_enabled is True

            state = engine.RuleState()
            states = {rule.name: state}
            engine.save_counts = lambda *_args, **_kwargs: None
            engine.save_evidence_image = lambda *_args, **_kwargs: None
            engine.play_sound = lambda current_rule: sound_calls.append(current_rule.name)
            engine.execute_action(rule, state, config, states)
            assert state.count == 1
            assert sound_calls == [rule.name]
    finally:
        engine.CONFIG_PATH = original_config_path
        engine.save_counts = original_save_counts
        engine.save_evidence_image = original_save_evidence
        engine.play_sound = original_play_sound

    print("Count and optional sound integration check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
