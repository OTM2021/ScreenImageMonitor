from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

import cv2
import mss
import numpy as np
import pytesseract
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import filedialog, messagebox, simpledialog, ttk


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
CONFIG_PATH = APP_DIR / "config.json"
COUNT_PATH = APP_DIR / "counts.json"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


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
    def __init__(self, parent: tk.Misc, monitor: dict[str, int]) -> None:
        self.result: dict[str, int] | None = None
        self.monitor = monitor
        self.original = capture_region(monitor)

        self.window = tk.Toplevel(parent)
        self.window.title("領域選択")
        self.window.grab_set()

        screen_w = max(900, parent.winfo_screenwidth() - 120)
        screen_h = max(600, parent.winfo_screenheight() - 180)
        scale = min(screen_w / self.original.width, screen_h / self.original.height, 1.0)
        self.scale = scale
        shown_size = (
            max(1, int(self.original.width * scale)),
            max(1, int(self.original.height * scale)),
        )
        shown = self.original.resize(shown_size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(shown)

        ttk.Label(
            self.window,
            text="マウスで対象範囲をドラッグしてください。Enterで確定、Escで取消します。",
        ).pack(fill="x", padx=8, pady=8)

        self.canvas = tk.Canvas(
            self.window,
            width=shown_size[0],
            height=shown_size[1],
            cursor="crosshair",
            highlightthickness=0,
        )
        self.canvas.pack(padx=8, pady=(0, 8))
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        self.start_x = 0
        self.start_y = 0
        self.end_x = 0
        self.end_y = 0
        self.rect_id: int | None = None

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.window.bind("<Return>", lambda _event: self._confirm())
        self.window.bind("<Escape>", lambda _event: self._cancel())
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        self.window.focus_force()

    def _press(self, event: tk.Event) -> None:
        self.start_x = max(0, min(int(event.x), self.photo.width()))
        self.start_y = max(0, min(int(event.y), self.photo.height()))
        self.end_x = self.start_x
        self.end_y = self.start_y
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.end_x,
            self.end_y,
            outline="#ff3030",
            width=3,
        )

    def _drag(self, event: tk.Event) -> None:
        self.end_x = max(0, min(int(event.x), self.photo.width()))
        self.end_y = max(0, min(int(event.y), self.photo.height()))
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

    def _confirm(self) -> None:
        x1, x2 = sorted((self.start_x, self.end_x))
        y1, y2 = sorted((self.start_y, self.end_y))
        if x2 - x1 < 3 or y2 - y1 < 3:
            messagebox.showwarning("領域選択", "3ピクセル以上の範囲を選択してください。", parent=self.window)
            return

        original_x1 = int(round(x1 / self.scale))
        original_y1 = int(round(y1 / self.scale))
        original_x2 = int(round(x2 / self.scale))
        original_y2 = int(round(y2 / self.scale))

        self.result = {
            "left": self.monitor["left"] + original_x1,
            "top": self.monitor["top"] + original_y1,
            "width": max(1, original_x2 - original_x1),
            "height": max(1, original_y2 - original_y1),
        }
        self.window.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.window.destroy()

    def show(self) -> dict[str, int] | None:
        self.window.wait_window()
        return self.result


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
        self.root.title("ScreenImageMonitor GUI設定")
        self.root.geometry("1080x720")
        self.root.minsize(980, 650)

        raw = load_json(config_path, {})
        if not isinstance(raw, dict):
            raw = {}
        self.config = ensure_absolute_config(raw)
        self.counts = load_json(APP_DIR / str(self.config.get("count_file", "counts.json")), {})
        if not isinstance(self.counts, dict):
            self.counts = {}

        with mss.mss() as capture:
            self.monitors = [dict(item) for item in capture.monitors[1:]]
        if not self.monitors:
            raise RuntimeError("モニターを取得できませんでした。")

        self.selected_rule_index: int | None = None
        self.preview_photo: ImageTk.PhotoImage | None = None
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

        ttk.Button(left, text="数値カウント追加", command=lambda: self._add_rule("number", "count")).grid(row=1, column=0, sticky="ew", pady=(8, 3))
        ttk.Button(left, text="数値音通知追加", command=lambda: self._add_rule("number", "sound")).grid(row=1, column=1, sticky="ew", pady=(8, 3))
        ttk.Button(left, text="画像カウント追加", command=lambda: self._add_rule("template", "count")).grid(row=2, column=0, sticky="ew", pady=3)
        ttk.Button(left, text="画像音通知追加", command=lambda: self._add_rule("template", "sound")).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Button(left, text="選択ルール削除", command=self._delete_rule).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(9, weight=1)

        ttk.Label(right, text="ルール名").grid(row=0, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.name_var).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(right, text="方式 / 動作").grid(row=1, column=0, sticky="w", pady=4)
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
        ttk.Button(region_frame, text="画面からドラッグ選択", command=self._select_region).grid(row=0, column=1, padx=(8, 0))

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
            command=self._register_template_file,
        ).grid(row=0, column=1, padx=(8, 0))

        self.options_frame = ttk.LabelFrame(right, text="判定条件", padding=8)
        self.options_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)
        self.options_frame.columnconfigure(1, weight=1)

        ttk.Label(self.options_frame, text="条件").grid(row=0, column=0, sticky="w", pady=3)
        self.operator_var = tk.StringVar(value="increase")
        self.operator_combo = ttk.Combobox(
            self.options_frame,
            textvariable=self.operator_var,
            state="readonly",
            values=("eq", "ne", "gt", "ge", "lt", "le", "between", "changed", "increase", "decrease"),
        )
        self.operator_combo.grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(self.options_frame, text="値 / 範囲").grid(row=1, column=0, sticky="w", pady=3)
        value_frame = ttk.Frame(self.options_frame)
        value_frame.grid(row=1, column=1, sticky="ew", pady=3)
        self.value_var = tk.StringVar(value="0")
        self.minimum_var = tk.StringVar(value="0")
        self.maximum_var = tk.StringVar(value="100")
        ttk.Entry(value_frame, textvariable=self.value_var, width=12).pack(side="left")
        ttk.Label(value_frame, text="  最小").pack(side="left")
        ttk.Entry(value_frame, textvariable=self.minimum_var, width=10).pack(side="left")
        ttk.Label(value_frame, text="  最大").pack(side="left")
        ttk.Entry(value_frame, textvariable=self.maximum_var, width=10).pack(side="left")

        ttk.Label(self.options_frame, text="画像一致率").grid(row=2, column=0, sticky="w", pady=3)
        self.threshold_var = tk.StringVar(value="0.90")
        ttk.Entry(self.options_frame, textvariable=self.threshold_var).grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Label(self.options_frame, text="連続一致回数").grid(row=3, column=0, sticky="w", pady=3)
        self.required_var = tk.StringVar(value="2")
        ttk.Entry(self.options_frame, textvariable=self.required_var).grid(row=3, column=1, sticky="ew", pady=3)

        self.evidence_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self.options_frame,
            text="動作時に証跡スクリーンショットを保存する",
            variable=self.evidence_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=3)

        action_frame = ttk.Frame(right)
        action_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(action_frame, text="現在の監視領域をプレビュー保存", command=self._capture_sample).pack(side="left")
        ttk.Button(action_frame, text="OCR／画像一致テスト", command=self._test_current).pack(side="left", padx=8)
        ttk.Button(action_frame, text="ルールへ反映", command=self._apply_fields).pack(side="left", padx=8)

        ttk.Label(right, text="プレビュー画像／テスト結果").grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 4))
        preview_frame = ttk.Frame(right, relief="sunken", borderwidth=1)
        preview_frame.grid(row=9, column=0, columnspan=2, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview_label = ttk.Label(preview_frame, text="画面から領域を選択後、取り込みまたはテストを実行してください。", anchor="center")
        self.preview_label.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.result_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.result_var, font=("Segoe UI", 11, "bold")).grid(row=10, column=0, columnspan=2, sticky="w", pady=6)

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="config.jsonへ保存", command=self._save_all).pack(side="right")
        ttk.Button(bottom, text="閉じる", command=self.root.destroy).pack(side="right", padx=8)

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
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return image is not None and image.size > 0

    def _refresh_rule_list(self) -> None:
        self.rule_list.delete(0, tk.END)
        for rule in self.config.get("rules", []):
            detector = "数字" if rule.get("detector") == "number" else "画像"
            action = "カウント" if rule.get("action") == "count" else "音"
            count = self.counts.get(rule.get("name", ""), 0) if action == "カウント" else "-"
            suffix = ""
            if detector == "画像" and not self._template_is_valid(rule):
                suffix = " [画像未登録]"
            self.rule_list.insert(
                tk.END,
                f"{rule.get('name', '(名称なし)')} [{detector}/{action}] ({count}){suffix}",
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
        action = str(rule.get("action", "count"))
        self.type_label.configure(
            text=f"{'数字OCR' if detector == 'number' else '画像一致'} / "
            f"{'カウントアップ' if action == 'count' else '音通知'}"
        )
        self.count_var.set(
            str(self.counts.get(rule.get("name", ""), 0))
            if action == "count"
            else "-"
        )
        self.region_value = (
            rule.get("region") if isinstance(rule.get("region"), dict) else None
        )
        self._update_region_text()
        condition = (
            rule.get("condition", {})
            if isinstance(rule.get("condition"), dict)
            else {}
        )
        self.operator_var.set(str(condition.get("operator", "increase")))
        self.value_var.set(str(condition.get("value", 0)))
        self.minimum_var.set(str(condition.get("minimum", 0)))
        self.maximum_var.set(str(condition.get("maximum", 100)))
        self.threshold_var.set(str(rule.get("match_threshold", 0.90)))
        self.required_var.set(str(rule.get("required_matches", 2)))
        self.evidence_var.set(bool(rule.get("save_evidence", action == "count")))
        self.result_var.set("")

        if detector == "template":
            self.template_label.grid()
            self.template_frame.grid()
            value = str(rule.get("template", "")).strip()
            self.template_var.set(value or "未登録")
            path = self._resolve_template_path(value)
            if path is not None and path.is_file():
                try:
                    with Image.open(path) as opened:
                        preview = opened.convert("RGB")
                        preview.load()
                    self._show_preview(preview)
                except OSError:
                    self.preview_label.configure(image="", text="登録画像を読み込めません。再登録してください。")
        else:
            self.template_label.grid_remove()
            self.template_frame.grid_remove()
            self.template_var.set("対象外")

    def _update_region_text(self) -> None:
        if not self.region_value:
            self.region_var.set("未設定")
            return
        r = self.region_value
        self.region_var.set(
            f"left={r['left']}, top={r['top']}, "
            f"width={r['width']}, height={r['height']}"
        )

    def _add_rule(self, detector: str, action: str) -> None:
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
            "action": action,
            "region": None,
            "required_matches": 2,
            "save_evidence": action == "count",
        }
        if action == "sound":
            rule["sound"] = "sounds/alert.wav"
        if detector == "number":
            rule["condition"] = {
                "operator": "increase" if action == "count" else "ge",
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
        monitor_index = max(0, self.monitor_combo.current())
        self.root.withdraw()
        self.root.update()
        time.sleep(0.25)
        try:
            selector = RegionSelector(self.root, self.monitors[monitor_index])
            region = selector.show()
        finally:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        if region:
            self.region_value = region
            self._update_region_text()
            image = capture_region(region)
            self._show_preview(image)
            self.result_var.set(
                "監視範囲を選択しました。ルールへ反映後、config.jsonへ保存してください。"
            )

    def _show_preview(self, image: Image.Image) -> None:
        shown = image.copy()
        shown.thumbnail((720, 300), Image.Resampling.LANCZOS)
        self.preview_photo = ImageTk.PhotoImage(shown)
        self.preview_label.configure(image=self.preview_photo, text="")

    def _current_rule(self) -> dict[str, Any] | None:
        if self.selected_rule_index is None:
            messagebox.showwarning(
                "設定",
                "ルールを選択してください。",
                parent=self.root,
            )
            return None
        return self.config["rules"][self.selected_rule_index]

    def _register_template_file(self) -> bool:
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
        self._show_preview(image)
        self.result_var.set(f"照合画像を登録しました: {relative}")
        self._refresh_rule_list()
        if self.selected_rule_index is not None:
            self.rule_list.selection_set(self.selected_rule_index)
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
        return self._register_template_file()

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
                "GUIの「画面からドラッグ選択」で監視範囲を指定してください。",
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
        rule["save_evidence"] = bool(self.evidence_var.get())

        if old_name != name and old_name in self.counts:
            self.counts[name] = self.counts.pop(old_name)

        if rule.get("detector") == "number":
            condition = rule.setdefault("condition", {})
            operator = self.operator_var.get()
            condition["operator"] = operator
            condition.setdefault("tolerance", 0)
            condition.setdefault("trigger_on_initial", False)
            try:
                if operator == "between":
                    condition["minimum"] = float(self.minimum_var.get())
                    condition["maximum"] = float(self.maximum_var.get())
                elif operator in {"eq", "ne", "gt", "ge", "lt", "le"}:
                    condition["value"] = float(self.value_var.get())
            except ValueError:
                messagebox.showerror(
                    "設定",
                    "数値条件に正しい数字を入力してください。",
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
        self._show_preview(image)
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
        self._show_preview(image)
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
            self.result_var.set(f"OCR結果: {text!r}")
            return

        template_path = self._resolve_template_path(rule.get("template"))
        if template_path is None or not template_path.is_file():
            self.result_var.set("PNG/JPEG画像を登録してください。")
            return
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
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
            minimum_score, _, _, _ = cv2.minMaxLoc(result)
            score = 1.0 - float(minimum_score)
        else:
            result = cv2.matchTemplate(
                current,
                template,
                cv2.TM_CCOEFF_NORMED,
            )
            _, score, _, _ = cv2.minMaxLoc(result)
        score = max(0.0, min(1.0, float(score)))
        self.result_var.set(
            f"画像一致率: {score:.4f}（設定値: {rule.get('match_threshold', 0.90)}）"
        )

    def _clear_selected_count(self) -> None:
        rule = self._current_rule()
        if rule is None or rule.get("action") != "count":
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
