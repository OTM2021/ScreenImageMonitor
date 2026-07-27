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

import screen_image_monitor as engine
from screen_setup_gui import (
    NUMBER_OPERATOR_LABELS,
    parse_range_input,
    test_ocr_condition,
)


def test_range_input() -> None:
    assert parse_range_input("1-10") == (1.0, 10.0)
    assert parse_range_input("120 ～ 129") == (120.0, 129.0)
    assert parse_range_input("-10--1") == (-10.0, -1.0)
    assert parse_range_input("1.5-2.75") == (1.5, 2.75)

    for invalid in ("", "1", "10-1", "a-b"):
        try:
            parse_range_input(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid range was accepted: {invalid!r}")


def test_contains_range_condition() -> None:
    condition_dict = {
        "operator": "contains_range",
        "minimum": 120,
        "maximum": 129,
        "tolerance": 0,
    }
    matched, value = test_ocr_condition("温度 121.1 C", condition_dict)
    assert matched is True
    assert value == 121.1

    matched, value = test_ocr_condition("温度 119.9 C", condition_dict)
    assert matched is False
    assert value is None

    condition = engine.NumberCondition(
        operator="contains_range",
        minimum=120,
        maximum=129,
    )
    assert engine.matching_number_for_condition(None, "121.1", condition) == 121.1
    assert engine.matching_number_for_condition(None, "119.9", condition) is None


def test_config_load() -> None:
    original_path = engine.CONFIG_PATH
    try:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "coordinate_mode": "absolute",
                        "check_interval_seconds": 0.5,
                        "show_status": True,
                        "count_file": "counts.json",
                        "evidence_dir": "evidence",
                        "rules": [
                            {
                                "name": "range-in-text",
                                "detector": "number",
                                "action": "count",
                                "region": {
                                    "left": 0,
                                    "top": 0,
                                    "width": 100,
                                    "height": 40,
                                },
                                "condition": {
                                    "operator": "contains_range",
                                    "minimum": 120,
                                    "maximum": 129,
                                },
                                "required_matches": 1,
                                "save_evidence": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            engine.CONFIG_PATH = path
            config = engine.load_config()
            condition = config.rules[0].number_condition
            assert condition is not None
            assert condition.operator == "contains_range"
            assert condition.minimum == 120
            assert condition.maximum == 129
    finally:
        engine.CONFIG_PATH = original_path


def test_japanese_ui_labels() -> None:
    assert NUMBER_OPERATOR_LABELS["指定範囲内"] == "between"
    assert (
        NUMBER_OPERATOR_LABELS["OCR結果に指定範囲の数値を含む"]
        == "contains_range"
    )


if __name__ == "__main__":
    test_range_input()
    test_contains_range_condition()
    test_config_load()
    test_japanese_ui_labels()
    print("Japanese number-condition UI and contains-range smoke test OK")
