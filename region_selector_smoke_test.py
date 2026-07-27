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
    expected_fixed_size: tuple[int, int] | None = None

    def __init__(
        self,
        root,
        monitor,
        *,
        fixed_size,
        use_parent_window,
        restore_parent,
    ) -> None:
        assert isinstance(root, DummyRoot)
        assert monitor == {"left": -1920, "top": 0, "width": 1920, "height": 1080}
        assert fixed_size == self.expected_fixed_size
        assert use_parent_window is True
        assert restore_parent is False

    def show(self):
        if self.expected_fixed_size is None:
            return {"left": -1800, "top": 100, "width": 320, "height": 120}
        width, height = self.expected_fixed_size
        return {"left": -1700, "top": 200, "width": width, "height": height}


class DummyCanvas:
    def __init__(self) -> None:
        self.last_coords: tuple[int, int, int, int] | None = None

    def coords(self, _item, x1, y1, x2, y2) -> None:
        self.last_coords = (x1, y1, x2, y2)


class DummyWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


def _run_helper(options: dict | None) -> dict:
    original_tk = setup_gui.tk.Tk
    original_selector = setup_gui.RegionSelector
    original_dpi = setup_gui.set_dpi_awareness
    try:
        setup_gui.tk.Tk = DummyRoot
        setup_gui.RegionSelector = DummySelector
        setup_gui.set_dpi_awareness = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "region.json"
            options_json = json.dumps(options) if options is not None else None
            exit_code = setup_gui.run_region_selector_helper(
                json.dumps({"left": -1920, "top": 0, "width": 1920, "height": 1080}),
                result_path,
                options_json,
            )
            assert exit_code == 0
            return json.loads(result_path.read_text(encoding="utf-8"))
    finally:
        setup_gui.tk.Tk = original_tk
        setup_gui.RegionSelector = original_selector
        setup_gui.set_dpi_awareness = original_dpi


def test_helper_writes_free_selected_region() -> None:
    DummySelector.expected_fixed_size = None
    payload = _run_helper(None)
    assert payload == {
        "status": "selected",
        "region": {"left": -1800, "top": 100, "width": 320, "height": 120},
    }


def test_helper_passes_template_fixed_size() -> None:
    DummySelector.expected_fixed_size = (240, 90)
    payload = _run_helper({"fixed_width": 240, "fixed_height": 90})
    assert payload == {
        "status": "selected",
        "region": {"left": -1700, "top": 200, "width": 240, "height": 90},
    }


def test_fixed_box_clamps_and_preserves_dimensions() -> None:
    selector = setup_gui.RegionSelector.__new__(setup_gui.RegionSelector)
    selector.monitor = {"left": -1920, "top": 0, "width": 1920, "height": 1080}
    selector.fixed_size = (320, 120)
    selector.rect_id = 1
    selector.canvas = DummyCanvas()
    selector.confirm_pending = True
    selector.window = DummyWindow()

    selector._update_fixed_box(1919, 1079)
    assert selector.fixed_left == 1600
    assert selector.fixed_top == 960
    assert selector.canvas.last_coords == (1600, 960, 1919, 1079)

    selector._confirm_fixed()
    assert selector.result == {
        "left": -320,
        "top": 960,
        "width": 320,
        "height": 120,
    }
    assert selector.window.destroyed is True


def test_exact_template_region_validation() -> None:
    app = setup_gui.SetupApp.__new__(setup_gui.SetupApp)
    app.root = None
    app._template_dimensions = lambda _rule: (240, 90)
    app.region_value = {"left": 0, "top": 0, "width": 240, "height": 90}
    assert app._validate_template_fits_region({}, show_error=False) is True
    app.region_value = {"left": 0, "top": 0, "width": 241, "height": 90}
    assert app._validate_template_fits_region({}, show_error=False) is False
    app.region_value = {"left": 0, "top": 0, "width": 200, "height": 90}
    assert app._validate_template_fits_region({}, show_error=False) is False


def test_main_dispatches_helper_mode() -> None:
    original = main_gui.run_region_selector_helper
    calls: list[tuple[str, str, str | None]] = []
    try:
        main_gui.run_region_selector_helper = (
            lambda monitor, output, options=None: calls.append((monitor, output, options)) or 7
        )
        options = '{"fixed_width":240,"fixed_height":90}'
        code = main_gui.main(["--region-selector-helper", "{}", "result.json", options])
        assert code == 7
        assert calls == [("{}", "result.json", options)]
    finally:
        main_gui.run_region_selector_helper = original


def test_labels_and_fixed_size_flow_are_present() -> None:
    setup_text = Path("screen_setup_gui.py").read_text(encoding="utf-8")
    main_text = Path("screen_image_monitor_gui.py").read_text(encoding="utf-8")
    assert "画面から範囲選択" in setup_text
    assert "画面から範囲選択" in main_text
    assert "fixed_width" in setup_text
    assert "fixed_height" in setup_text
    assert "登録画像と同じ" in setup_text
    assert "--region-selector-helper" in setup_text
    assert "--region-selector-helper" in main_text


if __name__ == "__main__":
    test_helper_writes_free_selected_region()
    test_helper_passes_template_fixed_size()
    test_fixed_box_clamps_and_preserves_dimensions()
    test_exact_template_region_validation()
    test_main_dispatches_helper_mode()
    test_labels_and_fixed_size_flow_are_present()
    print("v6.5 fixed-size region selector smoke test OK")
