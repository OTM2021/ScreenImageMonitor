from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

import cv2
import mss
import numpy as np
import pytesseract
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk
from tkinter import filedialog, messagebox, simpledialog, ttk

from image_file_io import read_cv_image


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
CONFIG_PATH = APP_DIR / "config.json"
COUNT_PATH = APP_DIR / "counts.json"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

NUMBER_OPERATOR_LABELS: dict[str, str] = {
    "指定した数値と同じ": "eq",
    "指定した数値と異なる": "ne",
    "指定した数値より大きい": "gt",
    "指定した数値以上": "ge",
    "指定した数値より小さい": "lt",
    "指定した数値以下": "le",
    "指定範囲内": "between",
    "OCR結果に指定範囲の数値を含む": "contains_range",
    "前回から数値が変わった": "changed",
    "前回より数値が増えた": "increase",
    "前回より数値が減った": "decrease",
}
NUMBER_OPERATOR_CODES = {code: label for label, code in NUMBER_OPERATOR_LABELS.items()}
VALUE_OPERATORS = {"eq", "ne", "gt", "ge", "lt", "le"}
RANGE_OPERATORS = {"between", "contains_range"}
NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
RANGE_INPUT_PATTERN = re.compile(
    r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:-|～|〜|~|,|，)\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)


def format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    return f"{number:g}"


def format_range(minimum: Any, maximum: Any) -> str:
    return f"{format_number(minimum)}-{format_number(maximum)}"


def parse_range_input(value: str) -> tuple[float, float]:
    normalized = value.strip().replace("．", ".")
    match = RANGE_INPUT_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError("範囲は 1-10 の形式で入力してください。")
    minimum = float(match.group(1))
    maximum = float(match.group(2))
    if minimum > maximum:
        raise ValueError("範囲の左側は右側以下にしてください。")
    return minimum, maximum


def parse_ocr_numbers(text: str) -> list[float]:
    values: list[float] = []
    for matched in NUMBER_PATTERN.findall(text):
        try:
            values.append(float(matched.replace(",", ".")))
        except ValueError:
            continue
    return values


def number_matches_threshold(value: float, condition: dict[str, Any]) -> bool:
    operator = str(condition.get("operator", "eq"))
    tolerance = float(condition.get("tolerance", 0) or 0)
    if operator == "eq":
        return abs(value - float(condition.get("value", 0))) <= tolerance
    if operator == "ne":
        return abs(value - float(condition.get("value", 0))) > tolerance
    if operator == "gt":
        return value > float(condition.get("value", 0)) + tolerance
    if operator == "ge":
        return value >= float(condition.get("value", 0)) - tolerance
    if operator == "lt":
        return value < float(condition.get("value", 0)) - tolerance
    if operator == "le":
        return value <= float(condition.get("value", 0)) + tolerance
    if operator in RANGE_OPERATORS:
        minimum = float(condition.get("minimum", 0))
        maximum = float(condition.get("maximum", 0))
        return minimum - tolerance <= value <= maximum + tolerance
    return False


def test_ocr_condition(
    text: str, condition: dict[str, Any], number_index: int = 0
) -> tuple[bool | None, float | None]:
    operator = str(condition.get("operator", "increase"))
    if operator in {"changed", "increase", "decrease"}:
        return None, None

    numbers = parse_ocr_numbers(text)
    if operator == "contains_range":
        for candidate in numbers:
            if number_matches_threshold(candidate, condition):
                return True, candidate
        return False, None

    if number_index < 0 or number_index >= len(numbers):
        return False, None
    candidate = numbers[number_index]
    return number_matches_threshold(candidate, condition), candidate


def set_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value.strip())
    value = re.sub(r"\s+", "_", value)
    return value or "rule"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(path)



def configure_tesseract() -> Path | None:
    candidates = [
        RESOURCE_DIR / "tesseract" / "tesseract.exe",
        APP_DIR / "tesseract" / "tesseract.exe",
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    ]
    found = shutil.which("tesseract")
    if found:
        candidates.append(Path(found))

    for candidate in candidates:
        if candidate.exists():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            tessdata = candidate.parent / "tessdata"
            if tessdata.exists():
                os.environ["TESSDATA_PREFIX"] = os.path.normpath(str(tessdata))
            return candidate
    return None


def ensure_absolute_config(raw: dict[str, Any]) -> dict[str, Any]:
    coordinate_mode = raw.get("coordinate_mode")
    if coordinate_mode is None:
        coordinate_mode = "relative" if isinstance(raw.get("monitor"), dict) else "absolute"
    raw["coordinate_mode"] = coordinate_mode
    raw.setdefault("check_interval_seconds", 0.5)
    raw.setdefault("show_status", True)
    raw.setdefault("count_file", "counts.json")
    raw.setdefault("evidence_dir", "evidence")
    raw.setdefault("rules", [])

    if raw.get("coordinate_mode") == "absolute":
        return raw

    monitor = raw.get("monitor", {})
    base_left = int(monitor.get("left", 0))
    base_top = int(monitor.get("top", 0))

    for rule in raw.get("rules", []):
        if not isinstance(rule, dict):
            continue
        region = rule.get("region")
        if isinstance(region, dict):
            region["left"] = int(region.get("left", 0)) + base_left
            region["top"] = int(region.get("top", 0)) + base_top
        elif rule.get("detector") == "template":
            rule["region"] = {
                "left": base_left,
                "top": base_top,
                "width": int(monitor.get("width", 800)),
                "height": int(monitor.get("height", 600)),
            }

    raw["coordinate_mode"] = "absolute"
    return raw


def capture_region(region: dict[str, int]) -> Image.Image:
    with mss.mss() as capture:
        shot = capture.grab(region)
        return Image.frombytes("RGB", shot.size, shot.rgb)


class RegionSelector:
    """Select a desktop region on a full-monitor overlay.

    Number OCR rules use free drag selection. Image rules may pass ``fixed_size``
    so the selector always returns a region with exactly the same width and
    height as the registered PNG/JPEG template.
    """

    def __init__(
        self,
        parent: tk.Misc,
        monitor: dict[str, int],
        *,
        fixed_size: tuple[int, int] | None = None,
        use_parent_window: bool = False,
        restore_parent: bool = True,
    ) -> None:
        self.result: dict[str, int] | None = None
        self.parent = parent.winfo_toplevel()
        self.restore_parent = restore_parent
        self.use_parent_window = use_parent_window
        self.monitor = {key: int(value) for key, value in monitor.items()}
        self.fixed_size = fixed_size
        if self.fixed_size is not None:
            fixed_width, fixed_height = self.fixed_size
            if fixed_width < 1 or fixed_height < 1:
                raise ValueError("fixed selection dimensions must be positive")
            if (
                fixed_width > self.monitor["width"]
                or fixed_height > self.monitor["height"]
            ):
                raise ValueError(
                    "registered image is larger than the selected monitor"
                )

        original = capture_region(self.monitor).convert("RGB")
        shade = Image.new("RGB", original.size, (0, 0, 0))
        self.original = Image.blend(original, shade, 0.30)

        if self.use_parent_window:
            self.window = self.parent
        else:
            self.window = tk.Toplevel(self.parent)
        self.window.withdraw()
        self.window.title("監視範囲を選択")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)

        width = self.monitor["width"]
        height = self.monitor["height"]
        left = self.monitor["left"]
        top = self.monitor["top"]
        self.window.geometry(f"{width}x{height}{left:+d}{top:+d}")

        self.photo = ImageTk.PhotoImage(self.original)
        self.canvas = tk.Canvas(
            self.window,
            width=width,
            height=height,
            cursor="crosshair",
            highlightthickness=0,
            borderwidth=0,
            background="#202020",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.create_rectangle(
            2,
            2,
            max(3, width - 3),
            max(3, height - 3),
            outline="#ff3030",
            width=4,
        )
        self.canvas.create_rectangle(
            16,
            16,
            min(width - 16, 920),
            78,
            fill="#101010",
            outline="#ffffff",
            width=2,
        )
        if self.fixed_size is None:
            instruction = (
                "監視する範囲をドラッグしてください。マウスを離すと確定します。"
                "Esc／右クリックで取消。"
            )
        else:
            fixed_width, fixed_height = self.fixed_size
            instruction = (
                f"登録画像と同じ {fixed_width} x {fixed_height} の枠を移動します。"
                "監視位置を左クリックで確定。Esc／右クリックで取消。"
            )
        self.canvas.create_text(
            32,
            47,
            anchor="w",
            fill="#ffffff",
            font=("Yu Gothic UI", 14, "bold"),
            text=instruction,
        )

        self.start_x = 0
        self.start_y = 0
        self.end_x = 0
        self.end_y = 0
        self.fixed_left = 0
        self.fixed_top = 0
        self.rect_id: int | None = None
        self.confirm_pending = False

        if self.fixed_size is None:
            self.canvas.bind("<ButtonPress-1>", self._press)
            self.canvas.bind("<B1-Motion>", self._drag)
            self.canvas.bind("<ButtonRelease-1>", self._release)
        else:
            self.rect_id = self.canvas.create_rectangle(
                0,
                0,
                1,
                1,
                outline="#00ff60",
                width=4,
            )
            self._update_fixed_box(width // 2, height // 2)
            self.canvas.bind("<Motion>", self._move_fixed)
            self.canvas.bind("<ButtonPress-1>", self._select_fixed)
        self.canvas.bind("<Button-3>", lambda _event: self._cancel())
        self.window.bind("<Escape>", lambda _event: self._cancel())
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)

    def _clamp_x(self, value: int) -> int:
        return max(0, min(value, self.monitor["width"] - 1))

    def _clamp_y(self, value: int) -> int:
        return max(0, min(value, self.monitor["height"] - 1))

    def _press(self, event: tk.Event) -> None:
        self.confirm_pending = False
        self.start_x = self._clamp_x(int(event.x))
        self.start_y = self._clamp_y(int(event.y))
        self.end_x = self.start_x
        self.end_y = self.start_y
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.end_x,
            self.end_y,
            outline="#00ff60",
            width=4,
        )

    def _drag(self, event: tk.Event) -> None:
        self.end_x = self._clamp_x(int(event.x))
        self.end_y = self._clamp_y(int(event.y))
        if self.rect_id is not None:
            self.canvas.coords(
                self.rect_id,
                self.start_x,
                self.start_y,
                self.end_x,
                self.end_y,
            )

    def _release(self, event: tk.Event) -> None:
        self._drag(event)
        x1, x2 = sorted((self.start_x, self.end_x))
        y1, y2 = sorted((self.start_y, self.end_y))
        if x2 - x1 < 3 or y2 - y1 < 3:
            messagebox.showwarning(
                "監視範囲",
                "3ピクセル以上の範囲を選択してください。",
                parent=self.window,
            )
            return
        self.confirm_pending = True
        self.window.after(60, self._confirm_free)

    def _confirm_free(self) -> None:
        if not self.confirm_pending:
            return
        x1, x2 = sorted((self.start_x, self.end_x))
        y1, y2 = sorted((self.start_y, self.end_y))
        self.result = {
            "left": self.monitor["left"] + x1,
            "top": self.monitor["top"] + y1,
            "width": max(1, x2 - x1),
            "height": max(1, y2 - y1),
        }
        self.window.destroy()

    def _confirm(self) -> None:
        """Backward-compatible alias for free-drag confirmation."""
        self._confirm_free()

    def _update_fixed_box(self, pointer_x: int, pointer_y: int) -> None:
        if self.fixed_size is None:
            return
        fixed_width, fixed_height = self.fixed_size
        max_left = self.monitor["width"] - fixed_width
        max_top = self.monitor["height"] - fixed_height
        self.fixed_left = max(0, min(int(pointer_x) - fixed_width // 2, max_left))
        self.fixed_top = max(0, min(int(pointer_y) - fixed_height // 2, max_top))
        if self.rect_id is not None:
            self.canvas.coords(
                self.rect_id,
                self.fixed_left,
                self.fixed_top,
                self.fixed_left + fixed_width - 1,
                self.fixed_top + fixed_height - 1,
            )

    def _move_fixed(self, event: tk.Event) -> None:
        self._update_fixed_box(int(event.x), int(event.y))

    def _select_fixed(self, event: tk.Event) -> None:
        self._move_fixed(event)
        self.confirm_pending = True
        self.window.after(60, self._confirm_fixed)

    def _confirm_fixed(self) -> None:
        if not self.confirm_pending or self.fixed_size is None:
            return
        fixed_width, fixed_height = self.fixed_size
        self.result = {
            "left": self.monitor["left"] + self.fixed_left,
            "top": self.monitor["top"] + self.fixed_top,
            "width": fixed_width,
            "height": fixed_height,
        }
        self.window.destroy()

    def _cancel(self) -> None:
        self.confirm_pending = False
        self.result = None
        if self.window.winfo_exists():
            self.window.destroy()

    def show(self) -> dict[str, int] | None:
        try:
            try:
                self.parent.grab_release()
            except tk.TclError:
                pass

            self.window.deiconify()
            self.window.update_idletasks()
            self.window.wait_visibility()
            self.window.attributes("-topmost", True)
            self.window.lift()
            self.window.focus_force()
            self.window.grab_set()
            self.window.after_idle(lambda: self.window.attributes("-topmost", True))
            if getattr(self, "use_parent_window", False):
                self.window.mainloop()
            else:
                self.window.wait_window()
            return self.result
        finally:
            if getattr(self, "restore_parent", True) and self.parent is not self.window:
                try:
                    if self.parent.winfo_exists():
                        self.parent.deiconify()
                        self.parent.lift()
                        self.parent.focus_force()
                        self.parent.grab_set()
                except tk.TclError:
                    pass


def run_region_selector_helper(
    monitor_json: str,
    result_path: str | Path,
    selector_options_json: str | None = None,
) -> int:
    """Run the range selector in a separate process."""
    output = Path(result_path)
    try:
        monitor_raw = json.loads(monitor_json)
        if not isinstance(monitor_raw, dict):
            raise ValueError("monitor must be a JSON object")
        monitor = {
            key: int(monitor_raw[key])
            for key in ("left", "top", "width", "height")
        }
        if monitor["width"] < 1 or monitor["height"] < 1:
            raise ValueError("monitor dimensions must be positive")

        options: dict[str, Any] = {}
        if selector_options_json:
            loaded_options = json.loads(selector_options_json)
            if not isinstance(loaded_options, dict):
                raise ValueError("selector options must be a JSON object")
            options = loaded_options

        fixed_size: tuple[int, int] | None = None
        if "fixed_width" in options or "fixed_height" in options:
            fixed_width = int(options.get("fixed_width", 0))
            fixed_height = int(options.get("fixed_height", 0))
            if fixed_width < 1 or fixed_height < 1:
                raise ValueError("fixed selection dimensions must be positive")
            fixed_size = (fixed_width, fixed_height)

        set_dpi_awareness()
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        selector = RegionSelector(
            root,
            monitor,
            fixed_size=fixed_size,
            use_parent_window=True,
            restore_parent=False,
        )
        region = selector.show()
        if region is not None:
            save_json(output, {"status": "selected", "region": region})
        else:
            save_json(output, {"status": "cancelled"})
        return 0
    except Exception as error:
        try:
            save_json(output, {"status": "error", "message": str(error)})
        except Exception:
            pass
        return 1


class SetupApp:
    def __init__(
        self,
        root: tk.Misc,
        config_path: Path,
        on_saved: Callable[[], None] | None = None,
        initial_rule_name: str | None = None,
        auto_select_region: bool = False,
    ) -> None:
        self.root = root
        self.config_path = config_path
        self.on_saved = on_saved
        self.initial_rule_name = initial_rule_name
        self.auto_select_region = auto_select_region
        self.root.title("ScreenImageMonitor v1.0 GUI設定")
        self.root.geometry("1080x720")
        self.root.minsize(980, 650)

        raw = load_json(config_path, {})
        if not isinstance(raw, dict):
            raw = {}
        self.config = ensure_absolute_config(raw)
        # v6.3: every detector counts; sound is an optional notification.
        # Migrate legacy sound-only rules without requiring manual recreation.
        for rule in self.config.get("rules", []):
            if not isinstance(rule, dict):
                continue
            legacy_action = rule.get("action", "count")
            rule["sound_enabled"] = bool(
                rule.get("sound_enabled", legacy_action == "sound")
            )
            rule["action"] = "count"
            rule.setdefault("sound", "sounds/alert.wav")
            rule.setdefault("save_evidence", True)
        self.counts = load_json(APP_DIR / str(self.config.get("count_file", "counts.json")), {})
        if not isinstance(self.counts, dict):
            self.counts = {}

        with mss.mss() as capture:
            self.monitors = [dict(item) for item in capture.monitors[1:]]
        if not self.monitors:
            raise RuntimeError("モニターを取得できませんでした。")

        self.selected_rule_index: int | None = None
        self.region_preview_photo: ImageTk.PhotoImage | None = None
        self.template_preview_photo: ImageTk.PhotoImage | None = None
        self.region_value: dict[str, int] | None = None

        self._build_ui()
        self._refresh_rule_list()
        if self.config["rules"]:
            selected_index = 0
            if self.initial_rule_name:
                for index, rule in enumerate(self.config["rules"]):
                    if str(rule.get("name", "")) == self.initial_rule_name:
                        selected_index = index
                        break
            self.rule_list.selection_set(selected_index)
            self.rule_list.see(selected_index)
            self._load_rule(selected_index)
            if self.auto_select_region:
                self.root.after(250, self._select_region)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(outer, text="監視ルール", padding=8)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.rowconfigure(0, weight=1)

        self.rule_list = tk.Listbox(left, width=31, exportselection=False)
        self.rule_list.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.rule_list.bind("<<ListboxSelect>>", self._on_rule_select)

        ttk.Button(
            left,
            text="数字OCRルール追加",
            command=lambda: self._add_rule("number"),
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 3))
        ttk.Button(
            left,
            text="画像一致ルール追加",
            command=lambda: self._add_rule("template"),
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(left, text="選択ルール削除", command=self._delete_rule).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(9, weight=1)

        ttk.Label(right, text="ルール名").grid(row=0, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.name_var).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(right, text="判定方式").grid(row=1, column=0, sticky="w", pady=4)
        self.type_label = ttk.Label(right, text="-")
        self.type_label.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(right, text="現在カウント").grid(row=2, column=0, sticky="w", pady=4)
        count_frame = ttk.Frame(right)
        count_frame.grid(row=2, column=1, sticky="w", pady=4)
        self.count_var = tk.StringVar(value="0")
        ttk.Label(count_frame, textvariable=self.count_var, font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(count_frame, text="このカウントをクリア", command=self._clear_selected_count).pack(side="left", padx=12)

        ttk.Label(right, text="選択対象モニター").grid(row=3, column=0, sticky="w", pady=4)
        self.monitor_combo = ttk.Combobox(right, state="readonly")
        self.monitor_combo["values"] = [
            f"Monitor {i + 1}: {m['width']}x{m['height']} ({m['left']},{m['top']})"
            for i, m in enumerate(self.monitors)
        ]
        self.monitor_combo.current(0)
        self.monitor_combo.grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Label(right, text="監視領域").grid(row=4, column=0, sticky="w", pady=4)
        region_frame = ttk.Frame(right)
        region_frame.grid(row=4, column=1, sticky="ew", pady=4)
        region_frame.columnconfigure(0, weight=1)
        self.region_var = tk.StringVar(value="未設定")
        ttk.Entry(region_frame, textvariable=self.region_var, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(region_frame, text="画面から範囲選択", command=self._select_region).grid(row=0, column=1, padx=(8, 0))

        self.template_label = ttk.Label(right, text="登録画像")
        self.template_label.grid(row=5, column=0, sticky="w", pady=4)
        self.template_frame = ttk.Frame(right)
        self.template_frame.grid(row=5, column=1, sticky="ew", pady=4)
        self.template_frame.columnconfigure(0, weight=1)
        self.template_var = tk.StringVar(value="未登録")
        ttk.Entry(self.template_frame, textvariable=self.template_var, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(
            self.template_frame,
            text="PNG/JPEGを登録...",
            command=lambda: self._register_template_file(select_region_after=True),
        ).grid(row=0, column=1, padx=(8, 0))

        self.options_frame = ttk.LabelFrame(right, text="判定条件", padding=8)
        self.options_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)
        self.options_frame.columnconfigure(0, weight=1)

        # 数字OCRと画像一致の設定欄は完全に分離する。
        # 画像ルールでOCR条件が表示されると、OCRが実行されるように見えるため。
        self.number_options_frame = ttk.Frame(self.options_frame)
        self.number_options_frame.grid(row=0, column=0, sticky="ew")
        self.number_options_frame.columnconfigure(1, weight=1)

        ttk.Label(self.number_options_frame, text="数値条件").grid(
            row=0, column=0, sticky="w", pady=3
        )
        self.operator_var = tk.StringVar(value=NUMBER_OPERATOR_CODES["increase"])
        self.operator_combo = ttk.Combobox(
            self.number_options_frame,
            textvariable=self.operator_var,
            state="readonly",
            values=tuple(NUMBER_OPERATOR_LABELS.keys()),
        )
        self.operator_combo.grid(row=0, column=1, sticky="ew", pady=3)
        self.operator_combo.bind(
            "<<ComboboxSelected>>", self._on_number_operator_changed
        )

        self.condition_input_frame = ttk.Frame(self.number_options_frame)
        self.condition_input_frame.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=3
        )
        self.condition_input_frame.columnconfigure(1, weight=1)
        self.condition_input_label = ttk.Label(
            self.condition_input_frame, text="判定値"
        )
        self.condition_input_label.grid(row=0, column=0, sticky="w")
        self.condition_input_var = tk.StringVar(value="0")
        self.condition_input_entry = ttk.Entry(
            self.condition_input_frame, textvariable=self.condition_input_var
        )
        self.condition_input_entry.grid(
            row=0, column=1, sticky="ew", padx=(12, 0)
        )

        self.condition_help_var = tk.StringVar()
        ttk.Label(
            self.number_options_frame,
            textvariable=self.condition_help_var,
            wraplength=620,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(3, 0))

        self.image_options_frame = ttk.Frame(self.options_frame)
        self.image_options_frame.grid(row=0, column=0, sticky="ew")
        self.image_options_frame.columnconfigure(1, weight=1)

        ttk.Label(self.image_options_frame, text="画像一致率").grid(row=0, column=0, sticky="w", pady=3)
        self.threshold_var = tk.StringVar(value="0.90")
        ttk.Entry(self.image_options_frame, textvariable=self.threshold_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(
            self.image_options_frame,
            text="登録したPNG/JPEGとの画像一致だけを判定します。数字OCRは実行しません。",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))

        common_options_frame = ttk.Frame(self.options_frame)
        common_options_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        common_options_frame.columnconfigure(1, weight=1)

        ttk.Label(common_options_frame, text="連続一致回数").grid(row=0, column=0, sticky="w", pady=3)
        self.required_var = tk.StringVar(value="2")
        ttk.Entry(common_options_frame, textvariable=self.required_var).grid(row=0, column=1, sticky="ew", pady=3)

        self.sound_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            common_options_frame,
            text="カウントアップ時に音通知する",
            variable=self.sound_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=3)

        self.evidence_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            common_options_frame,
            text="カウントアップ時に証跡スクリーンショットを保存する",
            variable=self.evidence_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=3)

        action_frame = ttk.Frame(right)
        action_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(action_frame, text="現在の監視領域をプレビュー保存", command=self._capture_sample).pack(side="left")
        self.test_button = ttk.Button(action_frame, text="判定テスト", command=self._test_current)
        self.test_button.pack(side="left", padx=8)
        ttk.Button(action_frame, text="ルールへ反映", command=self._apply_fields).pack(side="left", padx=8)

        ttk.Label(right, text="監視範囲と登録画像／テスト結果").grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 4))
        preview_frame = ttk.Frame(right)
        preview_frame.grid(row=9, column=0, columnspan=2, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.columnconfigure(1, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        region_preview_box = ttk.LabelFrame(preview_frame, text="監視範囲の現在画像", padding=6)
        region_preview_box.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        region_preview_box.columnconfigure(0, weight=1)
        region_preview_box.rowconfigure(0, weight=1)
        self.region_preview_label = ttk.Label(
            region_preview_box,
            text="画面から監視範囲を選択してください。",
            anchor="center",
        )
        self.region_preview_label.grid(row=0, column=0, sticky="nsew")

        template_preview_box = ttk.LabelFrame(preview_frame, text="登録したPNG/JPEG", padding=6)
        template_preview_box.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        template_preview_box.columnconfigure(0, weight=1)
        template_preview_box.rowconfigure(0, weight=1)
        self.template_preview_label = ttk.Label(
            template_preview_box,
            text="画像ルールではPNG/JPEGを登録してください。",
            anchor="center",
        )
        self.template_preview_label.grid(row=0, column=0, sticky="nsew")
        self.result_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.result_var, font=("Segoe UI", 11, "bold")).grid(row=10, column=0, columnspan=2, sticky="w", pady=6)

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="config.jsonへ保存", command=self._save_all).pack(side="right")
        ttk.Button(bottom, text="閉じる", command=self.root.destroy).pack(side="right", padx=8)
        self._update_number_condition_ui()

    def _number_operator_code(self) -> str:
        selected = self.operator_var.get()
        if selected in NUMBER_OPERATOR_LABELS:
            return NUMBER_OPERATOR_LABELS[selected]
        if selected in NUMBER_OPERATOR_CODES:
            return selected
        return "increase"

    def _set_number_operator(self, operator: str) -> None:
        self.operator_var.set(
            NUMBER_OPERATOR_CODES.get(operator, NUMBER_OPERATOR_CODES["increase"])
        )
        self._update_number_condition_ui()

    def _on_number_operator_changed(
        self, _event: tk.Event | None = None
    ) -> None:
        self._update_number_condition_ui()

    def _update_number_condition_ui(self) -> None:
        operator = self._number_operator_code()
        if operator in VALUE_OPERATORS:
            self.condition_input_frame.grid()
            self.condition_input_label.configure(text="判定値")
            try:
                float(self.condition_input_var.get())
            except ValueError:
                self.condition_input_var.set("0")
            self.condition_help_var.set(
                "例: 10。OCRで読み取った数値を、この値と比較します。"
            )
            return

        if operator == "between":
            self.condition_input_frame.grid()
            self.condition_input_label.configure(text="範囲")
            try:
                parse_range_input(self.condition_input_var.get())
            except ValueError:
                self.condition_input_var.set("1-10")
            self.condition_help_var.set(
                "範囲は 1-10 のように入力します。読み取った数値が範囲内なら検知します。"
            )
            return

        if operator == "contains_range":
            self.condition_input_frame.grid()
            self.condition_input_label.configure(text="含まれる範囲")
            try:
                parse_range_input(self.condition_input_var.get())
            except ValueError:
                self.condition_input_var.set("120-129")
            self.condition_help_var.set(
                "OCR結果に含まれるすべての数値を確認します。例: 121.1 は 120-129 で検知します。"
            )
            return

        self.condition_input_frame.grid_remove()
        descriptions = {
            "changed": "安定して読み取った数値が前回から変わったときに検知します。",
            "increase": "安定して読み取った数値が前回より増えたときに検知します。",
            "decrease": "安定して読み取った数値が前回より減ったときに検知します。",
        }
        self.condition_help_var.set(descriptions.get(operator, ""))

    def _resolve_template_path(self, value: Any) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value.strip())
        if not path.is_absolute():
            path = APP_DIR / path
        return path.resolve()

    def _template_is_valid(self, rule: dict[str, Any]) -> bool:
        path = self._resolve_template_path(rule.get("template"))
        if path is None or path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
            return False
        image = read_cv_image(path, cv2.IMREAD_GRAYSCALE)
        return image is not None and image.size > 0

    def _refresh_rule_list(self) -> None:
        self.rule_list.delete(0, tk.END)
        for rule in self.config.get("rules", []):
            detector = "数字" if rule.get("detector") == "number" else "画像"
            sound_suffix = "＋音" if bool(rule.get("sound_enabled", False)) else ""
            count = self.counts.get(rule.get("name", ""), 0)
            suffix = ""
            if detector == "画像" and not self._template_is_valid(rule):
                suffix = " [画像未登録]"
            self.rule_list.insert(
                tk.END,
                f"{rule.get('name', '(名称なし)')} [{detector}/カウント{sound_suffix}] ({count}){suffix}",
            )

    def _on_rule_select(self, _event: tk.Event | None = None) -> None:
        selection = self.rule_list.curselection()
        if not selection:
            return
        self._load_rule(int(selection[0]))

    def _load_rule(self, index: int) -> None:
        self.selected_rule_index = index
        rule = self.config["rules"][index]
        self.name_var.set(str(rule.get("name", "")))
        detector = str(rule.get("detector", "number"))
        self.type_label.configure(
            text="数字OCR" if detector == "number" else "画像一致"
        )
        self.count_var.set(str(self.counts.get(rule.get("name", ""), 0)))
        self.region_value = (
            rule.get("region") if isinstance(rule.get("region"), dict) else None
        )
        self._update_region_text()
        self._select_monitor_for_region(self.region_value)
        if self.region_value is not None:
            try:
                self._show_region_preview(capture_region(self.region_value))
            except Exception as error:
                self._clear_region_preview(f"監視範囲を取得できません: {error}")
        else:
            self._clear_region_preview()
        condition = (
            rule.get("condition", {})
            if isinstance(rule.get("condition"), dict)
            else {}
        )
        operator = str(condition.get("operator", "increase"))
        if operator in VALUE_OPERATORS:
            self.condition_input_var.set(format_number(condition.get("value", 0)))
        elif operator in RANGE_OPERATORS:
            self.condition_input_var.set(
                format_range(
                    condition.get("minimum", 0),
                    condition.get("maximum", 100),
                )
            )
        else:
            self.condition_input_var.set("")
        self._set_number_operator(operator)
        self.threshold_var.set(str(rule.get("match_threshold", 0.90)))
        self.required_var.set(str(rule.get("required_matches", 2)))
        self.sound_var.set(bool(rule.get("sound_enabled", False)))
        self.evidence_var.set(bool(rule.get("save_evidence", True)))
        self.result_var.set("")

        if detector == "template":
            self.template_label.grid()
            self.template_frame.grid()
            self.number_options_frame.grid_remove()
            self.image_options_frame.grid()
            self.options_frame.configure(text="画像一致判定条件")
            self.test_button.configure(text="画像一致テスト")
            value = str(rule.get("template", "")).strip()
            self.template_var.set(value or "未登録")
            path = self._resolve_template_path(value)
            if path is not None and path.is_file():
                try:
                    with Image.open(path) as opened:
                        preview = opened.convert("RGB")
                        preview.load()
                    self._show_template_preview(preview)
                except OSError:
                    self._clear_template_preview("登録画像を読み込めません。再登録してください。")
        else:
            self.template_label.grid_remove()
            self.template_frame.grid_remove()
            self.template_var.set("対象外")
            self._clear_template_preview("数字OCRルールでは登録画像を使用しません。")
            self.image_options_frame.grid_remove()
            self.number_options_frame.grid()
            self.options_frame.configure(text="数字OCR判定条件")
            self.test_button.configure(text="数字OCRテスト")

    def _update_region_text(self) -> None:
        if not self.region_value:
            self.region_var.set("未設定")
            return
        r = self.region_value
        self.region_var.set(
            f"left={r['left']}, top={r['top']}, "
            f"width={r['width']}, height={r['height']}"
        )

    def _select_monitor_for_region(self, region: dict[str, int] | None) -> None:
        if not region:
            return
        center_x = int(region.get("left", 0)) + int(region.get("width", 1)) // 2
        center_y = int(region.get("top", 0)) + int(region.get("height", 1)) // 2

        best_index = 0
        best_overlap = -1
        for index, monitor in enumerate(self.monitors):
            left = int(monitor["left"])
            top = int(monitor["top"])
            right = left + int(monitor["width"])
            bottom = top + int(monitor["height"])
            if left <= center_x < right and top <= center_y < bottom:
                self.monitor_combo.current(index)
                return

            overlap_left = max(left, int(region.get("left", 0)))
            overlap_top = max(top, int(region.get("top", 0)))
            overlap_right = min(right, int(region.get("left", 0)) + int(region.get("width", 1)))
            overlap_bottom = min(bottom, int(region.get("top", 0)) + int(region.get("height", 1)))
            overlap = max(0, overlap_right - overlap_left) * max(0, overlap_bottom - overlap_top)
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index
        self.monitor_combo.current(best_index)

    def _template_dimensions(self, rule: dict[str, Any]) -> tuple[int, int] | None:
        path = self._resolve_template_path(rule.get("template"))
        if path is None or not path.is_file():
            return None
        try:
            with Image.open(path) as opened:
                return opened.width, opened.height
        except OSError:
            return None

    def _validate_template_fits_region(
        self,
        rule: dict[str, Any],
        *,
        show_error: bool,
    ) -> bool:
        """Require an image-rule region to match the template dimensions exactly."""
        if self.region_value is None:
            return True
        dimensions = self._template_dimensions(rule)
        if dimensions is None:
            return True
        template_width, template_height = dimensions
        region_width = int(self.region_value["width"])
        region_height = int(self.region_value["height"])
        matches = (
            template_width == region_width
            and template_height == region_height
        )
        if not matches and show_error:
            messagebox.showerror(
                "画像と監視範囲のサイズが一致しません",
                "画像一致ルールの監視範囲は、登録画像と同じサイズにしてください。\n\n"
                f"監視範囲: {region_width} x {region_height}\n"
                f"登録画像: {template_width} x {template_height}\n\n"
                "「画面から範囲選択」を押すと、登録画像と同じサイズの枠で位置を指定できます。",
                parent=self.root,
            )
        return matches

    def _add_rule(self, detector: str) -> None:
        name = simpledialog.askstring(
            "ルール追加",
            "ルール名を入力してください。",
            parent=self.root,
        )
        if not name:
            return
        if any(rule.get("name") == name for rule in self.config["rules"]):
            messagebox.showerror(
                "ルール追加",
                "同じ名前のルールがあります。",
                parent=self.root,
            )
            return
        rule: dict[str, Any] = {
            "name": name,
            "detector": detector,
            "action": "count",
            "sound_enabled": False,
            "sound": "sounds/alert.wav",
            "region": None,
            "required_matches": 2,
            "save_evidence": True,
        }
        if detector == "number":
            rule["condition"] = {
                "operator": "increase",
                "value": 100,
                "tolerance": 0,
                "trigger_on_initial": False,
            }
            rule["ocr"] = {
                "psm": 7,
                "scale": 3.0,
                "threshold": "otsu",
                "invert": False,
                "whitelist": "0123456789.-",
                "timeout_seconds": 2.0,
                "number_index": 0,
                "border": 10,
            }
        else:
            rule["template"] = ""
            rule["match_threshold"] = 0.90
            rule["release_threshold"] = 0.75
        self.config["rules"].append(rule)
        self._refresh_rule_list()
        index = len(self.config["rules"]) - 1
        self.rule_list.selection_clear(0, tk.END)
        self.rule_list.selection_set(index)
        self.rule_list.see(index)
        self._load_rule(index)
        if detector == "template":
            self.root.after(100, self._prompt_template_registration)

    def _prompt_template_registration(self) -> None:
        rule = self._current_rule()
        if rule is None or rule.get("detector") != "template":
            return
        messagebox.showinfo(
            "画像登録が必要です",
            "画像識別ルールにはPNGまたはJPEG画像の事前登録が必要です。\n"
            "続けて照合画像を選択してください。",
            parent=self.root,
        )
        self._register_template_file()

    def _delete_rule(self) -> None:
        if self.selected_rule_index is None:
            return
        rule = self.config["rules"][self.selected_rule_index]
        if not messagebox.askyesno(
            "ルール削除",
            f"「{rule.get('name')}」を削除しますか？",
            parent=self.root,
        ):
            return
        del self.config["rules"][self.selected_rule_index]
        self.selected_rule_index = None
        self._refresh_rule_list()
        if self.config["rules"]:
            self.rule_list.selection_set(0)
            self._load_rule(0)

    def _select_region(self) -> None:
        if self.selected_rule_index is None:
            messagebox.showwarning(
                "監視範囲",
                "先に監視ルールを選択してください。",
                parent=self.root,
            )
            return

        rule = self.config["rules"][self.selected_rule_index]
        monitor_index = max(0, self.monitor_combo.current())
        monitor = self.monitors[monitor_index]
        selector_options: dict[str, int] = {}

        if rule.get("detector") == "template":
            if not self._ensure_template_registered(rule, prompt=True):
                self.result_var.set(
                    "画像一致ルールでは、先にPNG/JPEG画像を登録してください。"
                )
                return
            dimensions = self._template_dimensions(rule)
            if dimensions is None:
                messagebox.showerror(
                    "監視範囲",
                    "登録画像のサイズを取得できません。画像を再登録してください。",
                    parent=self.root,
                )
                return
            template_width, template_height = dimensions
            if (
                template_width > int(monitor["width"])
                or template_height > int(monitor["height"])
            ):
                messagebox.showerror(
                    "監視範囲",
                    "登録画像が選択対象モニターより大きいため、範囲を指定できません。\n\n"
                    f"モニター: {monitor['width']} x {monitor['height']}\n"
                    f"登録画像: {template_width} x {template_height}\n\n"
                    "別のモニターを選択するか、登録画像を変更してください。",
                    parent=self.root,
                )
                return
            selector_options = {
                "fixed_width": template_width,
                "fixed_height": template_height,
            }

        result_path = Path(tempfile.gettempdir()) / (
            f"screen_image_monitor_region_{uuid.uuid4().hex}.json"
        )

        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [
                sys.executable,
                str(Path(__file__).resolve().with_name("screen_image_monitor_gui.py")),
            ]
        command.extend(
            [
                "--region-selector-helper",
                json.dumps(monitor, ensure_ascii=False),
                str(result_path),
                json.dumps(selector_options, ensure_ascii=False),
            ]
        )

        try:
            try:
                self.root.grab_release()
            except tk.TclError:
                pass
            self.root.withdraw()
            self.root.update_idletasks()

            completed = subprocess.run(
                command,
                check=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            messagebox.showerror(
                "監視範囲",
                "画面から範囲選択がタイムアウトしました。",
                parent=self.root,
            )
            return
        except OSError as error:
            messagebox.showerror(
                "監視範囲",
                "画面から範囲選択を起動できませんでした。\n"
                f"{error}",
                parent=self.root,
            )
            return
        finally:
            self.root.deiconify()
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()
            try:
                self.root.grab_set()
            except tk.TclError:
                pass

        payload = load_json(result_path, {})
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass

        if not isinstance(payload, dict):
            payload = {}
        status = payload.get("status")
        if status == "cancelled":
            self.result_var.set("画面から範囲選択をキャンセルしました。")
            return
        if completed.returncode != 0 or status == "error":
            detail = str(payload.get("message", "不明なエラー"))
            messagebox.showerror(
                "監視範囲",
                "画面から範囲選択を起動できませんでした。\n"
                f"{detail}",
                parent=self.root,
            )
            return

        region = payload.get("region")
        if not isinstance(region, dict):
            messagebox.showerror(
                "監視範囲",
                "選択結果を取得できませんでした。",
                parent=self.root,
            )
            return
        try:
            region = {
                key: int(region[key])
                for key in ("left", "top", "width", "height")
            }
        except (KeyError, TypeError, ValueError):
            messagebox.showerror(
                "監視範囲",
                "選択結果の座標が不正です。",
                parent=self.root,
            )
            return

        self.region_value = region
        rule["region"] = dict(region)
        self._update_region_text()
        image = capture_region(region)
        self._show_region_preview(image)
        if rule.get("detector") == "template":
            if not self._validate_template_fits_region(rule, show_error=True):
                self.region_value = None
                rule["region"] = None
                self._update_region_text()
                self._clear_region_preview()
                return
            self.result_var.set(
                f"登録画像と同じ {region['width']} x {region['height']} の監視位置を指定しました。"
                "ルールへ反映後、config.jsonへ保存してください。"
            )
            return
        self.result_var.set(
            f"監視範囲を選択しました: {region['width']} x {region['height']}。"
            "ルールへ反映後、config.jsonへ保存してください。"
        )

    def _set_preview(
        self,
        label: ttk.Label,
        photo_attribute: str,
        image: Image.Image,
    ) -> None:
        shown = image.copy()
        shown.thumbnail((500, 280), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(shown)
        setattr(self, photo_attribute, photo)
        label.configure(image=photo, text="")

    def _show_region_preview(self, image: Image.Image) -> None:
        self._set_preview(
            self.region_preview_label,
            "region_preview_photo",
            image,
        )

    def _show_template_preview(self, image: Image.Image) -> None:
        self._set_preview(
            self.template_preview_label,
            "template_preview_photo",
            image,
        )

    def _clear_region_preview(self, text: str = "監視範囲を選択してください。") -> None:
        self.region_preview_photo = None
        self.region_preview_label.configure(image="", text=text)

    def _clear_template_preview(self, text: str) -> None:
        self.template_preview_photo = None
        self.template_preview_label.configure(image="", text=text)

    def _current_rule(self) -> dict[str, Any] | None:
        if self.selected_rule_index is None:
            messagebox.showwarning(
                "設定",
                "ルールを選択してください。",
                parent=self.root,
            )
            return None
        return self.config["rules"][self.selected_rule_index]

    def _register_template_file(
        self,
        *,
        select_region_after: bool = True,
    ) -> bool:
        rule = self._current_rule()
        if rule is None or rule.get("detector") != "template":
            messagebox.showinfo(
                "画像登録",
                "画像識別ルールを選択してください。",
                parent=self.root,
            )
            return False

        source_value = filedialog.askopenfilename(
            parent=self.root,
            title="照合に使用するPNG/JPEG画像を選択",
            filetypes=(
                ("画像ファイル", "*.png *.jpg *.jpeg"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg *.jpeg"),
            ),
        )
        if not source_value:
            self.result_var.set("画像登録がキャンセルされました。監視開始前に登録してください。")
            return False

        source = Path(source_value)
        if source.suffix.lower() not in IMAGE_EXTENSIONS:
            messagebox.showerror(
                "画像登録",
                "登録できる形式はPNG、JPG、JPEGです。",
                parent=self.root,
            )
            return False
        try:
            with Image.open(source) as opened:
                image = opened.convert("RGB")
                image.load()
        except OSError as error:
            messagebox.showerror(
                "画像登録",
                f"画像を読み込めません。\n\n{error}",
                parent=self.root,
            )
            return False

        destination_dir = APP_DIR / "templates"
        destination_dir.mkdir(parents=True, exist_ok=True)
        rule_name = safe_filename(self.name_var.get() or str(rule.get("name", "rule")))
        original_name = safe_filename(source.stem)
        destination = destination_dir / f"{rule_name}_{original_name}{source.suffix.lower()}"
        serial = 2
        while destination.exists() and source.resolve() != destination.resolve():
            destination = destination_dir / (
                f"{rule_name}_{original_name}_{serial}{source.suffix.lower()}"
            )
            serial += 1
        try:
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
        except OSError as error:
            messagebox.showerror(
                "画像登録",
                f"画像を登録フォルダーへコピーできません。\n\n{error}",
                parent=self.root,
            )
            return False

        relative = str(destination.relative_to(APP_DIR)).replace("\\", "/")
        rule["template"] = relative
        self.template_var.set(relative)
        self._show_template_preview(image)

        region_matches = (
            self.region_value is not None
            and int(self.region_value["width"]) == image.width
            and int(self.region_value["height"]) == image.height
        )
        if not region_matches:
            self.region_value = None
            rule["region"] = None
            self._update_region_text()
            self._clear_region_preview(
                f"登録画像と同じ {image.width} x {image.height} の監視位置を指定してください。"
            )

        self._refresh_rule_list()
        if self.selected_rule_index is not None:
            self.rule_list.selection_set(self.selected_rule_index)

        if not region_matches and select_region_after:
            self.result_var.set(
                f"照合画像を登録しました: {relative}\n"
                f"続けて、登録画像と同じ {image.width} x {image.height} の監視位置を指定します。"
            )
            self.root.after(150, self._select_region)
        elif region_matches:
            self.result_var.set(
                f"照合画像を登録しました: {relative}\n"
                f"既存の監視範囲は画像と同じ {image.width} x {image.height} です。"
            )
        else:
            self.result_var.set(
                f"照合画像を登録しました: {relative}\n"
                "「画面から範囲選択」で画像と同じサイズの監視位置を指定してください。"
            )
        return True

    def _ensure_template_registered(
        self,
        rule: dict[str, Any],
        prompt: bool = True,
    ) -> bool:
        if self._template_is_valid(rule):
            return True
        if not prompt:
            return False
        name = str(rule.get("name", "画像ルール"))
        register = messagebox.askyesno(
            "画像が未登録です",
            f"「{name}」に照合用のPNG/JPEG画像が登録されていません。\n\n"
            "今すぐ登録しますか？",
            parent=self.root,
        )
        if not register:
            return False
        return self._register_template_file(select_region_after=False)

    def _apply_fields(self, show_message: bool = True) -> bool:
        rule = self._current_rule()
        if rule is None:
            return False
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror(
                "設定",
                "ルール名を入力してください。",
                parent=self.root,
            )
            return False
        if self.region_value is None:
            messagebox.showerror(
                "設定",
                "GUIの「画面から範囲選択」で監視範囲を指定してください。",
                parent=self.root,
            )
            return False
        try:
            required = int(self.required_var.get())
            if required <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "設定",
                "連続一致回数は1以上の整数にしてください。",
                parent=self.root,
            )
            return False

        old_name = str(rule.get("name", ""))
        rule["name"] = name
        rule["region"] = dict(self.region_value)
        rule["required_matches"] = required
        rule["action"] = "count"
        rule["sound_enabled"] = bool(self.sound_var.get())
        rule["sound"] = "sounds/alert.wav"
        rule["save_evidence"] = bool(self.evidence_var.get())

        if old_name != name and old_name in self.counts:
            self.counts[name] = self.counts.pop(old_name)

        if rule.get("detector") == "number":
            condition = rule.setdefault("condition", {})
            operator = self._number_operator_code()
            condition["operator"] = operator
            condition.setdefault("tolerance", 0)
            condition.setdefault("trigger_on_initial", False)
            try:
                if operator in RANGE_OPERATORS:
                    minimum, maximum = parse_range_input(
                        self.condition_input_var.get()
                    )
                    condition["minimum"] = minimum
                    condition["maximum"] = maximum
                    condition.pop("value", None)
                elif operator in VALUE_OPERATORS:
                    condition["value"] = float(self.condition_input_var.get())
                    condition.pop("minimum", None)
                    condition.pop("maximum", None)
                else:
                    condition.pop("value", None)
                    condition.pop("minimum", None)
                    condition.pop("maximum", None)
            except ValueError as error:
                messagebox.showerror(
                    "設定",
                    str(error)
                    if str(error)
                    else "数値条件に正しい数字を入力してください。",
                    parent=self.root,
                )
                return False
        else:
            if not self._ensure_template_registered(rule, prompt=True):
                self.result_var.set("画像識別ルールにはPNG/JPEG画像の登録が必要です。")
                return False
            try:
                threshold = float(self.threshold_var.get())
                if not 0.0 <= threshold <= 1.0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "設定",
                    "画像一致率は0～1で入力してください。",
                    parent=self.root,
                )
                return False
            if not self._validate_template_fits_region(rule, show_error=True):
                return False
            rule["match_threshold"] = threshold
            rule["release_threshold"] = min(
                float(rule.get("release_threshold", 0.75)),
                max(0.0, threshold - 0.05),
            )

        self._refresh_rule_list()
        if self.selected_rule_index is not None:
            self.rule_list.selection_set(self.selected_rule_index)
        if show_message:
            messagebox.showinfo(
                "設定",
                "選択ルールへ反映しました。最後にconfig.jsonへ保存してください。",
                parent=self.root,
            )
        return True

    def _capture_sample(self) -> None:
        rule = self._current_rule()
        if rule is None or self.region_value is None:
            messagebox.showwarning(
                "プレビュー",
                "先に監視範囲を選択してください。",
                parent=self.root,
            )
            return
        image = capture_region(self.region_value)
        self._show_region_preview(image)
        name = safe_filename(str(rule.get("name", "rule")))
        path = APP_DIR / "samples" / f"{name}_current.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        rule["sample_image"] = str(path.relative_to(APP_DIR)).replace("\\", "/")
        self.result_var.set(
            f"現在の監視範囲をプレビュー保存しました: {rule['sample_image']}\n"
            "この画像は照合用テンプレートには使用されません。"
        )

    def _test_current(self) -> None:
        rule = self._current_rule()
        if rule is None or self.region_value is None:
            messagebox.showwarning(
                "テスト",
                "先に監視範囲を選択してください。",
                parent=self.root,
            )
            return
        if not self._apply_fields(show_message=False):
            return
        image = capture_region(self.region_value)
        self._show_region_preview(image)
        array = np.asarray(image)

        if rule.get("detector") == "number":
            if configure_tesseract() is None:
                messagebox.showerror(
                    "OCRテスト",
                    "Tesseract OCRが見つかりません。",
                    parent=self.root,
                )
                return
            options = rule.get("ocr", {})
            gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
            scale = float(options.get("scale", 3.0))
            gray = cv2.resize(
                gray,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_CUBIC,
            )
            threshold = options.get("threshold", "otsu")
            if threshold == "otsu":
                _, gray = cv2.threshold(
                    gray,
                    0,
                    255,
                    cv2.THRESH_BINARY | cv2.THRESH_OTSU,
                )
            elif threshold == "adaptive":
                gray = cv2.adaptiveThreshold(
                    gray,
                    255,
                    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY,
                    31,
                    11,
                )
            if options.get("invert", False):
                gray = cv2.bitwise_not(gray)
            whitelist = str(options.get("whitelist", "0123456789.-"))
            psm = int(options.get("psm", 7))
            text = pytesseract.image_to_string(
                gray,
                lang="eng",
                config=(
                    f"--oem 1 --psm {psm} "
                    f"-c tessedit_char_whitelist={whitelist}"
                ),
            ).strip()
            numbers = parse_ocr_numbers(text)
            condition = rule.get("condition", {})
            number_index = int(options.get("number_index", 0))
            matched, matched_number = test_ocr_condition(
                text, condition, number_index
            )
            number_text = ", ".join(f"{value:g}" for value in numbers) or "なし"
            if matched is None:
                judgement = "変化条件は監視開始後に前回値と比較して判定します。"
            elif matched:
                judgement = f"条件に一致しました（該当値: {matched_number:g}）。"
            else:
                judgement = "条件には一致しませんでした。"
            self.result_var.set(
                f"OCR結果: {text!r} / 読み取った数値: {number_text} / {judgement}"
            )
            return

        template_path = self._resolve_template_path(rule.get("template"))
        if template_path is None or not template_path.is_file():
            self.result_var.set("PNG/JPEG画像を登録してください。")
            return
        template = read_cv_image(template_path, cv2.IMREAD_GRAYSCALE)
        current = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        if template is None:
            self.result_var.set(f"登録画像を読み込めません: {template_path}")
            return
        if template.shape[0] > current.shape[0] or template.shape[1] > current.shape[1]:
            self.result_var.set(
                "登録画像が監視範囲より大きいため比較できません。監視範囲を広げてください。"
            )
            return
        if float(np.std(template)) < 1e-6:
            result = cv2.matchTemplate(
                current,
                template,
                cv2.TM_SQDIFF_NORMED,
            )
            minimum_score, _, minimum_location, _ = cv2.minMaxLoc(result)
            score = 1.0 - float(minimum_score)
            location = minimum_location
        else:
            result = cv2.matchTemplate(
                current,
                template,
                cv2.TM_CCOEFF_NORMED,
            )
            _, score, _, maximum_location = cv2.minMaxLoc(result)
            location = maximum_location
        score = max(0.0, min(1.0, float(score)))

        annotated = image.copy()
        draw = ImageDraw.Draw(annotated)
        x, y = location
        template_height, template_width = template.shape[:2]
        draw.rectangle(
            (x, y, x + template_width - 1, y + template_height - 1),
            outline="#ff2020",
            width=3,
        )
        self._show_region_preview(annotated)
        self.result_var.set(
            f"画像一致率: {score:.4f}（設定値: {rule.get('match_threshold', 0.90)}） / "
            f"検出位置: x={x}, y={y} / "
            f"監視範囲: {image.width}x{image.height} / "
            f"登録画像: {template_width}x{template_height}"
        )

    def _clear_selected_count(self) -> None:
        rule = self._current_rule()
        if rule is None:
            return
        name = str(rule.get("name", ""))
        self.counts[name] = 0
        save_json(
            APP_DIR / str(self.config.get("count_file", "counts.json")),
            self.counts,
        )
        self.count_var.set("0")
        self._refresh_rule_list()
        if self.selected_rule_index is not None:
            self.rule_list.selection_set(self.selected_rule_index)
        messagebox.showinfo(
            "カウントクリア",
            f"「{name}」を0にしました。",
            parent=self.root,
        )

    def _save_all(self) -> None:
        if self.selected_rule_index is not None and not self._apply_fields(show_message=False):
            return

        for index, rule in enumerate(self.config.get("rules", [])):
            if rule.get("detector") != "template" or self._template_is_valid(rule):
                continue
            self.rule_list.selection_clear(0, tk.END)
            self.rule_list.selection_set(index)
            self.rule_list.see(index)
            self._load_rule(index)
            if not self._ensure_template_registered(rule, prompt=True):
                messagebox.showerror(
                    "保存できません",
                    f"「{rule.get('name')}」にPNG/JPEG画像を登録してください。",
                    parent=self.root,
                )
                return

        self.config["coordinate_mode"] = "absolute"
        save_json(self.config_path, self.config)
        save_json(
            APP_DIR / str(self.config.get("count_file", "counts.json")),
            self.counts,
        )
        if self.on_saved is not None:
            self.on_saved()
        messagebox.showinfo(
            "保存完了",
            f"設定を保存しました。\n{self.config_path}",
            parent=self.root,
        )



def open_setup_window(
    parent: tk.Misc,
    config_path: Path | None = None,
    on_saved: Callable[[], None] | None = None,
    initial_rule_name: str | None = None,
    auto_select_region: bool = False,
) -> None:
    """Open the setup UI as a modal window owned by the main application."""
    set_dpi_awareness()
    window = tk.Toplevel(parent)
    try:
        SetupApp(
            window,
            config_path or CONFIG_PATH,
            on_saved=on_saved,
            initial_rule_name=initial_rule_name,
            auto_select_region=auto_select_region,
        )
        window.transient(parent)
        window.grab_set()
        window.focus_force()
        parent.wait_window(window)
    except Exception as error:
        messagebox.showerror("ScreenImageMonitor 設定", str(error), parent=window)
        if window.winfo_exists():
            window.destroy()
        raise


def run_setup_gui(config_path: Path | None = None) -> None:
    set_dpi_awareness()
    root = tk.Tk()
    try:
        SetupApp(root, config_path or CONFIG_PATH)
        root.mainloop()
    except Exception as error:
        messagebox.showerror("ScreenImageMonitor GUI設定", str(error), parent=root)
        root.destroy()
        raise


if __name__ == "__main__":
    run_setup_gui()
