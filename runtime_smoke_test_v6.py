from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np

try:
    import mss as _mss  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("mss")
    stub.mss = lambda: None
    sys.modules["mss"] = stub

import screen_image_monitor as engine
import screen_image_monitor_gui as gui
from screen_setup_gui import RegionSelector, SetupApp


class DummyWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


class DummyParent:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def grab_release(self) -> None:
        self.actions.append("grab_release")

    def winfo_exists(self) -> bool:
        return True

    def deiconify(self) -> None:
        self.actions.append("deiconify")

    def lift(self) -> None:
        self.actions.append("lift")

    def focus_force(self) -> None:
        self.actions.append("focus_force")

    def grab_set(self) -> None:
        self.actions.append("grab_set")


class DummySelectorWindow:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def deiconify(self) -> None:
        self.actions.append("deiconify")

    def update_idletasks(self) -> None:
        self.actions.append("update_idletasks")

    def wait_visibility(self) -> None:
        self.actions.append("wait_visibility")

    def attributes(self, *_args) -> None:
        self.actions.append("attributes")

    def lift(self) -> None:
        self.actions.append("lift")

    def focus_force(self) -> None:
        self.actions.append("focus_force")

    def grab_set(self) -> None:
        self.actions.append("grab_set")

    def after_idle(self, callback) -> None:
        self.actions.append("after_idle")
        callback()

    def wait_window(self) -> None:
        self.actions.append("wait_window")


class DummyCombo:
    def __init__(self) -> None:
        self.value = -1

    def current(self, value: int | None = None) -> int:
        if value is not None:
            self.value = value
        return self.value


class FakeShot:
    def __init__(self) -> None:
        self._array = np.zeros((12, 16, 4), dtype=np.uint8)

    def __array__(self, dtype=None):
        if dtype is None:
            return self._array
        return self._array.astype(dtype)


class FakeCapture:
    def __enter__(self) -> "FakeCapture":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def grab(self, _region):
        return FakeShot()


def test_exact_region_coordinates() -> None:
    selector = object.__new__(RegionSelector)
    selector.monitor = {
        "left": -1920,
        "top": 120,
        "width": 1920,
        "height": 1080,
    }
    selector.start_x = 100
    selector.start_y = 200
    selector.end_x = 340
    selector.end_y = 280
    selector.confirm_pending = True
    selector.result = None
    selector.window = DummyWindow()

    RegionSelector._confirm(selector)
    assert selector.result == {
        "left": -1820,
        "top": 320,
        "width": 240,
        "height": 80,
    }
    assert selector.window.destroyed


def test_selector_is_made_visible_before_grab() -> None:
    selector = object.__new__(RegionSelector)
    selector.parent = DummyParent()
    selector.window = DummySelectorWindow()
    selector.result = {"left": 1, "top": 2, "width": 3, "height": 4}

    result = RegionSelector.show(selector)
    assert result == selector.result
    actions = selector.window.actions
    assert actions.index("deiconify") < actions.index("wait_visibility")
    assert actions.index("wait_visibility") < actions.index("grab_set")
    assert selector.parent.actions[0] == "grab_release"
    assert selector.parent.actions[-1] == "grab_set"


def test_monitor_is_selected_from_saved_region() -> None:
    app = object.__new__(SetupApp)
    app.monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": -1920, "top": 0, "width": 1920, "height": 1080},
    ]
    app.monitor_combo = DummyCombo()

    SetupApp._select_monitor_for_region(
        app,
        {"left": -1500, "top": 100, "width": 300, "height": 200},
    )
    assert app.monitor_combo.value == 1


def test_rules_run_independently() -> None:
    original_mss = gui.mss.mss
    original_template_match = engine.calculate_template_match
    original_eval_template = engine.evaluate_template_rule
    original_recognize = engine.recognize_number
    original_eval_number = engine.evaluate_number_rule

    calls = {"template": 0, "number": 0}
    calls_lock = threading.Lock()

    def fake_template_match(_screen, _template):
        with calls_lock:
            calls["template"] += 1
        return 0.2, (1, 2), (3, 4)

    def fake_eval_template(rule, state, score, config, states, evidence_image=None):
        state.target_is_present = score > 0.9

    def fake_recognize(_frame, _rule):
        time.sleep(0.25)
        with calls_lock:
            calls["number"] += 1
        return 10.0, "10"

    def fake_eval_number(rule, state, number, raw_text, config, states, evidence_image=None):
        state.target_is_present = False

    gui.mss.mss = lambda: FakeCapture()
    engine.calculate_template_match = fake_template_match
    engine.evaluate_template_rule = fake_eval_template
    engine.recognize_number = fake_recognize
    engine.evaluate_number_rule = fake_eval_number

    try:
        worker = gui.MonitorWorker(gui.queue.Queue())
        region = engine.ScreenRegion(left=0, top=0, width=16, height=12)
        template_rule = SimpleNamespace(
            name="image",
            detector="template",
            action="count",
            sound_enabled=False,
            template_region=region,
            number_region=None,
            template_path=Path("template.png"),
        )
        number_rule = SimpleNamespace(
            name="number",
            detector="number",
            action="count",
            sound_enabled=False,
            template_region=None,
            number_region=region,
        )
        config = SimpleNamespace(check_interval_seconds=0.20)
        states = {
            "image": engine.RuleState(),
            "number": engine.RuleState(),
        }
        templates = {"image": np.zeros((3, 3), dtype=np.uint8)}
        status_map: dict[str, dict] = {}

        threads = [
            threading.Thread(
                target=worker._run_rule_loop,
                args=(template_rule, config, states, templates, status_map),
                daemon=True,
            ),
            threading.Thread(
                target=worker._run_rule_loop,
                args=(number_rule, config, states, templates, status_map),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.68)
        worker.stop_event.set()
        for thread in threads:
            thread.join(timeout=1.5)

        assert worker.rule_error_queue.empty(), "rule loop raised an exception"
        assert calls["template"] >= 5, calls
        assert calls["number"] >= 2, calls
        assert calls["template"] > calls["number"], calls
        assert status_map["image"]["scan_ms"] >= 0
        assert status_map["number"]["scan_ms"] >= 200
    finally:
        gui.mss.mss = original_mss
        engine.calculate_template_match = original_template_match
        engine.evaluate_template_rule = original_eval_template
        engine.recognize_number = original_recognize
        engine.evaluate_number_rule = original_eval_number


if __name__ == "__main__":
    test_exact_region_coordinates()
    test_selector_is_made_visible_before_grab()
    test_monitor_is_selected_from_saved_region()
    test_rules_run_independently()
    print("v6.2 runtime smoke test OK")
