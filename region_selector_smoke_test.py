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

import screen_image_monitor_gui as main_gui
import screen_setup_gui as setup_gui


class DummyRoot:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def withdraw(self) -> None:
        self.actions.append("withdraw")

    def update_idletasks(self) -> None:
        self.actions.append("update_idletasks")


class DummySelector:
    def __init__(self, root, monitor, *, use_parent_window, restore_parent) -> None:
        assert isinstance(root, DummyRoot)
        assert monitor == {"left": -1920, "top": 0, "width": 1920, "height": 1080}
        assert use_parent_window is True
        assert restore_parent is False

    def show(self):
        return {"left": -1800, "top": 100, "width": 320, "height": 120}


def test_helper_writes_selected_region() -> None:
    original_tk = setup_gui.tk.Tk
    original_selector = setup_gui.RegionSelector
    original_dpi = setup_gui.set_dpi_awareness
    try:
        setup_gui.tk.Tk = DummyRoot
        setup_gui.RegionSelector = DummySelector
        setup_gui.set_dpi_awareness = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "region.json"
            exit_code = setup_gui.run_region_selector_helper(
                json.dumps({"left": -1920, "top": 0, "width": 1920, "height": 1080}),
                result_path,
            )
            assert exit_code == 0
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            assert payload == {
                "status": "selected",
                "region": {"left": -1800, "top": 100, "width": 320, "height": 120},
            }
    finally:
        setup_gui.tk.Tk = original_tk
        setup_gui.RegionSelector = original_selector
        setup_gui.set_dpi_awareness = original_dpi


def test_main_dispatches_helper_mode() -> None:
    original = main_gui.run_region_selector_helper
    calls: list[tuple[str, str]] = []
    try:
        main_gui.run_region_selector_helper = lambda monitor, output: calls.append((monitor, output)) or 7
        code = main_gui.main(["--region-selector-helper", "{}", "result.json"])
        assert code == 7
        assert calls == [("{}", "result.json")]
    finally:
        main_gui.run_region_selector_helper = original


def test_labels_are_unified() -> None:
    setup_text = Path("screen_setup_gui.py").read_text(encoding="utf-8")
    main_text = Path("screen_image_monitor_gui.py").read_text(encoding="utf-8")
    assert "画面から範囲選択" in setup_text
    assert "画面から範囲選択" in main_text
    assert "画面からドラッグ選択" not in setup_text
    assert "選択ルールの監視範囲を指定" not in main_text
    assert "--region-selector-helper" in setup_text
    assert "--region-selector-helper" in main_text


if __name__ == "__main__":
    test_helper_writes_selected_region()
    test_main_dispatches_helper_mode()
    test_labels_are_unified()
    print("v6.3 region selector smoke test OK")
