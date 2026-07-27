from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import mss
import numpy as np
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

import screen_image_monitor as engine
from screen_setup_gui import (
    open_setup_window,
    run_region_selector_helper,
    set_dpi_awareness,
)


APP_DIR = engine.APP_DIR
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

COUNTER_DISPLAY_PATH = APP_DIR / "counter_display.json"
COUNTER_BACKGROUND = "#3F80EC"
COUNTER_FOREGROUND = "#FFFFFF"


@dataclass(frozen=True)
class CounterDisplaySettings:
    font_family: str = "Segoe UI Light"
    font_size: int = 56
    font_weight: str = "normal"


def load_counter_display_settings(
    path: Path = COUNTER_DISPLAY_PATH,
) -> CounterDisplaySettings:
    default = CounterDisplaySettings()
    if not path.exists():
        return default

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default

    if not isinstance(raw, dict):
        return default

    family = raw.get("font_family", default.font_family)
    size = raw.get("font_size", default.font_size)
    weight = raw.get("font_weight", default.font_weight)

    if not isinstance(family, str) or not family.strip():
        family = default.font_family
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = default.font_size
    size = max(18, min(size, 180))
    if weight not in {"normal", "bold"}:
        weight = default.font_weight

    return CounterDisplaySettings(
        font_family=family.strip(),
        font_size=size,
        font_weight=weight,
    )


def save_counter_display_settings(
    settings: CounterDisplaySettings,
    path: Path = COUNTER_DISPLAY_PATH,
) -> None:
    payload = {
        "font_family": settings.font_family,
        "font_size": settings.font_size,
        "font_weight": settings.font_weight,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def resolve_counter_font_family(widget: tk.Misc, requested: str) -> str:
    try:
        available = {str(name).casefold(): str(name) for name in tkfont.families(widget)}
    except tk.TclError:
        return requested
    for candidate in (requested, "Segoe UI Light", "Segoe UI", "Arial"):
        resolved = available.get(candidate.casefold())
        if resolved:
            return resolved
    return requested


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    payload: Any = None


class MonitorWorker:
    """Run every monitoring rule independently from the Tk event loop.

    OCR invokes an external Tesseract process and can take considerably longer
    than image matching.  A single sequential loop therefore allowed one slow
    OCR rule to delay every other rule.  Each rule now has its own worker loop
    and capture context, so image rules and other OCR rules keep running while
    one OCR call is in progress.
    """

    def __init__(self, event_queue: queue.Queue[WorkerEvent]) -> None:
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self.command_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.state_lock = threading.RLock()
        self.status_lock = threading.Lock()
        self.rule_error_queue: queue.Queue[tuple[BaseException, str]] = queue.Queue()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.stop_event.clear()
        while not self.rule_error_queue.empty():
            try:
                self.rule_error_queue.get_nowait()
            except queue.Empty:
                break
        self.thread = threading.Thread(
            target=self._run,
            name="ScreenImageMonitorCoordinator",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def request_clear(self, rule_name: str | None = None) -> None:
        self.command_queue.put(("clear", rule_name))

    def _put(self, kind: str, payload: Any = None) -> None:
        self.event_queue.put(WorkerEvent(kind, payload))

    def _log_listener(self, line: str) -> None:
        self._put("log", line)

    def _process_commands(
        self,
        config: engine.AppConfig,
        states: dict[str, engine.RuleState],
    ) -> None:
        while True:
            try:
                command, value = self.command_queue.get_nowait()
            except queue.Empty:
                return

            if command == "clear":
                with self.state_lock:
                    engine.clear_counts(config, states, rule_name=value)
                    counts = {
                        name: state.count
                        for name, state in states.items()
                    }
                self._put("counts", counts)

    @staticmethod
    def _region_for_rule(rule: engine.Rule) -> engine.ScreenRegion:
        region = (
            rule.template_region
            if rule.detector == "template"
            else rule.number_region
        )
        if region is None:
            raise ValueError(f"ルール「{rule.name}」の監視領域が未設定です。")
        return region

    def _run_rule_loop(
        self,
        rule: engine.Rule,
        config: engine.AppConfig,
        states: dict[str, engine.RuleState],
        templates: dict[str, np.ndarray],
        status_map: dict[str, dict[str, Any]],
    ) -> None:
        try:
            region = self._region_for_rule(rule)
            target_interval = (
                min(config.check_interval_seconds, 0.10)
                if rule.detector == "template"
                else config.check_interval_seconds
            )
            next_deadline = time.monotonic()
            scan_sequence = 0
            with mss.mss() as capture:
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if now < next_deadline:
                        if self.stop_event.wait(next_deadline - now):
                            break

                    cycle_start = time.monotonic()
                    screenshot = capture.grab(engine.region_to_dict(region))
                    frame = np.asarray(screenshot)

                    metric: str
                    detail: str
                    if rule.detector == "template":
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
                        score, location, _size = engine.calculate_template_match(
                            gray,
                            templates[rule.name],
                        )
                        with self.state_lock:
                            state = states[rule.name]
                            engine.evaluate_template_rule(
                                rule,
                                state,
                                score,
                                config,
                                states,
                                evidence_image=frame,
                            )
                            active = state.target_is_present
                            count = state.count
                        metric = f"{score:.3f}"
                        template_name = (
                            rule.template_path.name
                            if rule.template_path is not None
                            else "未登録"
                        )
                        detail = (
                            f"PNG/JPEG一致: {template_name} / "
                            f"位置 x={location[0]}, y={location[1]}"
                        )
                    else:
                        number, raw_text = engine.recognize_number(frame, rule)
                        with self.state_lock:
                            state = states[rule.name]
                            engine.evaluate_number_rule(
                                rule,
                                state,
                                number,
                                raw_text,
                                config,
                                states,
                                evidence_image=frame,
                            )
                            active = state.target_is_present
                            count = state.count
                        metric = "---" if number is None else f"{number:g}"
                        detail = f"OCR: {raw_text}" if raw_text else "OCR待機"

                    finished = time.monotonic()
                    elapsed_ms = int(round((finished - cycle_start) * 1000))
                    scan_sequence += 1
                    wall_time = time.time()
                    scan_time = (
                        time.strftime("%H:%M:%S", time.localtime(wall_time))
                        + f".{int((wall_time % 1) * 1000):03d}"
                    )
                    row = {
                        "name": rule.name,
                        "detector": rule.detector,
                        "action": rule.action,
                        "sound_enabled": rule.sound_enabled,
                        "metric": metric,
                        "detail": detail,
                        "active": active,
                        "count": count,
                        "scan_time": scan_time,
                        "scan_ms": elapsed_ms,
                        "scan_seq": scan_sequence,
                    }
                    with self.status_lock:
                        status_map[rule.name] = row

                    # Keep the requested cadence when processing is fast.  If
                    # OCR takes longer than the interval, start the next scan
                    # immediately rather than adding another full interval.
                    next_deadline += target_interval
                    if next_deadline < finished:
                        next_deadline = finished
        except BaseException as error:
            detail = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            self.rule_error_queue.put((error, detail))
            self.stop_event.set()

    def _run(self) -> None:
        engine.add_log_listener(self._log_listener)
        states: dict[str, engine.RuleState] = {}
        rule_threads: list[threading.Thread] = []

        try:
            config = engine.load_config()
            if not config.rules:
                raise ValueError("監視ルールがありません。設定画面でルールを追加してください。")

            saved_counts = engine.load_counts(config.count_file)
            templates: dict[str, np.ndarray] = {}
            status_map: dict[str, dict[str, Any]] = {}

            if any(rule.detector == "number" for rule in config.rules):
                tesseract_path = engine.configure_tesseract()
                engine.log(f"Tesseract OCR: {tesseract_path}")

            for rule in config.rules:
                if rule.detector == "template":
                    templates[rule.name] = engine.load_template(rule)
                states[rule.name] = engine.RuleState(
                    count=saved_counts.get(rule.name, 0)
                )
                status_map[rule.name] = {
                    "name": rule.name,
                    "detector": rule.detector,
                    "action": rule.action,
                    "sound_enabled": rule.sound_enabled,
                    "metric": "---",
                    "detail": "監視開始待ち",
                    "active": False,
                    "count": states[rule.name].count,
                    "scan_time": "---",
                    "scan_ms": 0,
                    "scan_seq": 0,
                }

            self._put(
                "started",
                {
                    "rules": [
                        {
                            "name": rule.name,
                            "detector": rule.detector,
                            "action": rule.action,
                            "sound_enabled": rule.sound_enabled,
                            "count": states[rule.name].count,
                        }
                        for rule in config.rules
                    ]
                },
            )
            engine.log(
                "GUI monitoring started. "
                f"Independent rule loops={len(config.rules)}, "
                f"interval={config.check_interval_seconds:g}s."
            )

            for rule in config.rules:
                thread = threading.Thread(
                    target=self._run_rule_loop,
                    args=(rule, config, states, templates, status_map),
                    name=f"MonitorRule-{rule.name}",
                    daemon=True,
                )
                rule_threads.append(thread)
                thread.start()

            next_status_time = 0.0
            while not self.stop_event.is_set():
                self._process_commands(config, states)

                try:
                    error, detail = self.rule_error_queue.get_nowait()
                except queue.Empty:
                    error = None
                    detail = ""
                if error is not None:
                    raise RuntimeError(str(error)) from error

                now = time.monotonic()
                if now >= next_status_time:
                    with self.status_lock:
                        rows = [dict(status_map[rule.name]) for rule in config.rules]
                    self._put("status", rows)
                    next_status_time = now + 0.20

                self.stop_event.wait(0.05)

            for thread in rule_threads:
                thread.join(timeout=3.5)

            try:
                error, detail = self.rule_error_queue.get_nowait()
            except queue.Empty:
                error = None
                detail = ""
            if error is not None:
                raise RuntimeError(str(error)) from error

            with self.state_lock:
                engine.save_counts(config.count_file, config.rules, states)
            engine.log("GUI monitoring stopped.")

        except Exception as error:
            self.stop_event.set()
            for thread in rule_threads:
                if thread.is_alive():
                    thread.join(timeout=0.5)
            detail = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            engine.log(f"ERROR: {error}")
            self._put("error", {"message": str(error), "detail": detail})
        finally:
            engine.remove_log_listener(self._log_listener)
            self.thread = None
            self._put("stopped")


class CounterFontDialog:
    """Choose the font used for the large counter values."""

    def __init__(
        self,
        parent: tk.Toplevel,
        settings: CounterDisplaySettings,
        on_apply: Callable[[CounterDisplaySettings], None],
    ) -> None:
        self.parent = parent
        self.on_apply = on_apply
        self.window = tk.Toplevel(parent)
        self.window.title("カウンターのフォント設定")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        families = sorted(
            {
                str(name)
                for name in tkfont.families(self.window)
                if str(name).strip() and not str(name).startswith("@")
            },
            key=str.casefold,
        )
        initial_family = resolve_counter_font_family(
            self.window,
            settings.font_family,
        )
        if initial_family not in families:
            families.insert(0, initial_family)

        self.family_var = tk.StringVar(value=initial_family)
        self.size_var = tk.IntVar(value=settings.font_size)
        self.bold_var = tk.BooleanVar(value=settings.font_weight == "bold")

        outer = ttk.Frame(self.window, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="フォント").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=4,
        )
        self.family_box = ttk.Combobox(
            outer,
            textvariable=self.family_var,
            values=families,
            width=34,
        )
        self.family_box.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(outer, text="文字サイズ").grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=4,
        )
        self.size_spin = ttk.Spinbox(
            outer,
            from_=18,
            to=180,
            increment=1,
            textvariable=self.size_var,
            width=8,
            command=self._refresh_preview,
        )
        self.size_spin.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Checkbutton(
            outer,
            text="太字にする",
            variable=self.bold_var,
            command=self._refresh_preview,
        ).grid(row=2, column=1, sticky="w", pady=(4, 8))

        ttk.Label(
            outer,
            text="Windowsにインストールされているフォントを使用します。",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))

        preview_frame = tk.Frame(
            outer,
            background=COUNTER_BACKGROUND,
            width=390,
            height=130,
        )
        preview_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        preview_frame.grid_propagate(False)
        preview_frame.rowconfigure(0, weight=1)
        preview_frame.columnconfigure(0, weight=1)
        self.preview_label = tk.Label(
            preview_frame,
            text="1234",
            foreground=COUNTER_FOREGROUND,
            background=COUNTER_BACKGROUND,
            anchor="center",
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        buttons = ttk.Frame(outer)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew")
        ttk.Button(
            buttons,
            text="初期設定に戻す",
            command=self._reset,
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="キャンセル",
            command=self.close,
        ).pack(side="right")
        ttk.Button(
            buttons,
            text="保存",
            command=self._save,
        ).pack(side="right", padx=(0, 8))

        self.family_box.bind("<<ComboboxSelected>>", self._refresh_preview)
        self.family_box.bind("<KeyRelease>", self._refresh_preview)
        self.size_spin.bind("<KeyRelease>", self._refresh_preview)
        self.window.bind("<Return>", lambda _event: self._save())
        self.window.bind("<Escape>", lambda _event: self.close())

        self._refresh_preview()
        self.window.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.window.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")
        self.window.wait_visibility()
        self.window.grab_set()
        self.family_box.focus_set()

    def _current_settings(self) -> CounterDisplaySettings:
        family = self.family_var.get().strip() or CounterDisplaySettings().font_family
        try:
            size = int(self.size_var.get())
        except (tk.TclError, ValueError):
            size = CounterDisplaySettings().font_size
        size = max(18, min(size, 180))
        return CounterDisplaySettings(
            font_family=family,
            font_size=size,
            font_weight="bold" if self.bold_var.get() else "normal",
        )

    def _refresh_preview(self, _event: tk.Event | None = None) -> None:
        settings = self._current_settings()
        family = resolve_counter_font_family(self.window, settings.font_family)
        self.preview_label.configure(
            font=(family, settings.font_size, settings.font_weight),
        )

    def _reset(self) -> None:
        default = CounterDisplaySettings()
        self.family_var.set(resolve_counter_font_family(self.window, default.font_family))
        self.size_var.set(default.font_size)
        self.bold_var.set(False)
        self._refresh_preview()

    def _save(self) -> None:
        settings = self._current_settings()
        try:
            save_counter_display_settings(settings)
        except OSError as error:
            messagebox.showerror(
                "フォント設定",
                f"フォント設定を保存できませんでした。\n\n{error}",
                parent=self.window,
            )
            return
        self.on_apply(settings)
        self.close()

    def close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        if self.window.winfo_exists():
            self.window.destroy()


class CounterWindow:
    """Independent, large-format counter display window."""

    def __init__(
        self,
        parent: tk.Tk,
        clear_one: Callable[[str], None],
        clear_all: Callable[[], None],
        on_close: Callable[[], None],
    ) -> None:
        self.parent = parent
        self.clear_one_callback = clear_one
        self.clear_all_callback = clear_all
        self.on_close_callback = on_close
        self.cards: dict[str, dict[str, tk.Widget]] = {}
        self.settings = load_counter_display_settings()

        self.window = tk.Toplevel(parent)
        self.window.title("ScreenImageMonitor カウンター")
        self.window.geometry("580x620")
        self.window.minsize(430, 300)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.counter_font = tkfont.Font(
            self.window,
            family=resolve_counter_font_family(
                self.window,
                self.settings.font_family,
            ),
            size=self.settings.font_size,
            weight=self.settings.font_weight,
        )

        outer = ttk.Frame(self.window, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="カウンター",
            font=("Yu Gothic UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            header,
            text="フォント設定",
            command=self.open_font_settings,
        ).grid(row=0, column=1, sticky="e")

        self.canvas = tk.Canvas(
            outer,
            highlightthickness=0,
            borderwidth=0,
            background="#F3F4F6",
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)

        self.card_container = tk.Frame(self.canvas, background="#F3F4F6")
        self.canvas_window = self.canvas.create_window(
            (0, 0),
            window=self.card_container,
            anchor="nw",
        )
        self.card_container.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._resize_card_container)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

        buttons = ttk.Frame(outer)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(
            buttons,
            text="全カウントをクリア",
            command=self.clear_all_callback,
        ).pack(side="left")
        ttk.Button(buttons, text="閉じる", command=self.close).pack(side="right")

    @property
    def exists(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def focus(self) -> None:
        if self.exists:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()

    def close(self) -> None:
        self._unbind_mousewheel()
        if self.exists:
            self.window.destroy()
        self.on_close_callback()

    def _update_scroll_region(self, _event: tk.Event | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_card_container(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=max(1, event.width))

    def _bind_mousewheel(self, _event: tk.Event | None = None) -> None:
        self.window.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: tk.Event | None = None) -> None:
        try:
            self.window.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass

    def _on_mousewheel(self, event: tk.Event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def open_font_settings(self) -> None:
        CounterFontDialog(
            self.window,
            self.settings,
            self.apply_font_settings,
        )

    def apply_font_settings(self, settings: CounterDisplaySettings) -> None:
        self.settings = settings
        self.counter_font.configure(
            family=resolve_counter_font_family(self.window, settings.font_family),
            size=settings.font_size,
            weight=settings.font_weight,
        )
        self._update_scroll_region()

    def _create_card(self, name: str, count: int = 0, state: str = "停止") -> None:
        if name in self.cards:
            return

        card = tk.Frame(
            self.card_container,
            background=COUNTER_BACKGROUND,
            padx=16,
            pady=12,
        )
        card.pack(fill="x", padx=4, pady=(0, 10))
        card.columnconfigure(0, weight=1)

        name_label = tk.Label(
            card,
            text=name,
            foreground=COUNTER_FOREGROUND,
            background=COUNTER_BACKGROUND,
            font=("Yu Gothic UI", 11, "bold"),
            anchor="w",
        )
        name_label.grid(row=0, column=0, sticky="ew")

        clear_button = tk.Button(
            card,
            text="0に戻す",
            command=lambda rule_name=name: self.clear_one_callback(rule_name),
            padx=10,
            pady=2,
        )
        clear_button.grid(row=0, column=1, sticky="e")

        count_label = tk.Label(
            card,
            text=str(count),
            foreground=COUNTER_FOREGROUND,
            background=COUNTER_BACKGROUND,
            font=self.counter_font,
            anchor="center",
            padx=8,
            pady=4,
        )
        count_label.grid(row=1, column=0, columnspan=2, sticky="nsew")

        state_label = tk.Label(
            card,
            text=state,
            foreground=COUNTER_FOREGROUND,
            background=COUNTER_BACKGROUND,
            font=("Yu Gothic UI", 10),
            anchor="e",
        )
        state_label.grid(row=2, column=0, columnspan=2, sticky="e")

        self.cards[name] = {
            "frame": card,
            "count": count_label,
            "state": state_label,
        }

    def set_rules(self, rules: list[engine.Rule], counts: dict[str, int]) -> None:
        for card in self.cards.values():
            card["frame"].destroy()
        self.cards.clear()

        for rule in rules:
            self._create_card(
                rule.name,
                count=counts.get(rule.name, 0),
                state="停止",
            )
        self._update_scroll_region()

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = str(row["name"])
            self._create_card(name)
            self.cards[name]["count"].configure(text=str(row.get("count", 0)))
            self.cards[name]["state"].configure(
                text="成立" if row.get("active") else "監視中",
            )

    def update_counts(self, counts: dict[str, int]) -> None:
        for name, value in counts.items():
            self._create_card(name)
            self.cards[name]["count"].configure(text=str(value))

    def set_stopped(self) -> None:
        for card in self.cards.values():
            card["state"].configure(text="停止")


class MainApplication:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ScreenImageMonitor v1.0")
        self.root.geometry("1120x760")
        self.root.minsize(920, 620)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.events: queue.Queue[WorkerEvent] = queue.Queue()
        self.worker = MonitorWorker(self.events)
        self.closing = False
        self.pending_settings: tuple[str | None, bool] | None = None
        self.counter_window: CounterWindow | None = None

        self.status_var = tk.StringVar(value="停止中")
        self.summary_var = tk.StringVar(value="設定を読み込んでいます。")

        self._build_ui()
        self.reload_configuration()
        self.root.after(100, self._poll_events)
        self.root.after(300, self.show_counter_window)

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        monitor_font = tkfont.Font(self.root, family="Yu Gothic UI", size=10)
        monitor_heading_font = tkfont.Font(
            self.root,
            family="Yu Gothic UI",
            size=10,
            weight="bold",
        )
        row_height = monitor_font.metrics("linespace") + 12
        style.configure(
            "Monitor.Treeview",
            font=monitor_font,
            rowheight=row_height,
        )
        style.configure(
            "Monitor.Treeview.Heading",
            font=monitor_heading_font,
        )


        header = ttk.Frame(self.root, padding=(12, 10))
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)

        ttk.Label(
            header,
            text="ScreenImageMonitor",
            font=("Segoe UI", 17, "bold"),
        ).grid(row=0, column=0, sticky="w")

        self.status_badge = ttk.Label(
            header,
            textvariable=self.status_var,
            font=("Segoe UI", 11, "bold"),
        )
        self.status_badge.grid(row=0, column=1, sticky="e")

        toolbar = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        toolbar.pack(fill="x")

        self.start_button = ttk.Button(
            toolbar,
            text="監視開始",
            command=self.start_monitoring,
        )
        self.start_button.pack(side="left")

        self.stop_button = ttk.Button(
            toolbar,
            text="監視停止",
            command=self.stop_monitoring,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=(8, 0))

        self.setup_button = ttk.Button(
            toolbar,
            text="設定",
            command=self.open_settings,
        )
        self.setup_button.pack(side="left", padx=(18, 0))

        ttk.Button(
            toolbar,
            text="画面から範囲選択",
            command=self.select_monitoring_region,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            toolbar,
            text="設定再読込",
            command=self.reload_configuration,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            toolbar,
            text="カウンター表示",
            command=self.show_counter_window,
        ).pack(side="left", padx=(18, 0))

        ttk.Button(
            toolbar,
            text="証跡画像を開く",
            command=self.open_evidence_folder,
        ).pack(side="right")

        summary = ttk.LabelFrame(self.root, text="監視状況", padding=8)
        summary.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        summary.columnconfigure(0, weight=1)
        summary.rowconfigure(0, weight=1)

        columns = (
            "detector",
            "action",
            "metric",
            "state",
            "detail",
        )
        self.tree = ttk.Treeview(
            summary,
            columns=columns,
            show="tree headings",
            selectmode="browse",
            style="Monitor.Treeview",
        )
        self.tree.heading("#0", text="ルール名")
        self.tree.heading("detector", text="判定")
        self.tree.heading("action", text="動作")
        self.tree.heading("metric", text="現在値／一致率")
        self.tree.heading("state", text="状態")
        self.tree.heading("detail", text="判定詳細")

        self.tree.column("#0", width=230, minwidth=150)
        self.tree.column("detector", width=100, anchor="center")
        self.tree.column("action", width=100, anchor="center")
        self.tree.column("metric", width=140, anchor="center")
        self.tree.column("state", width=100, anchor="center")
        self.tree.column("detail", width=330)

        scrollbar = ttk.Scrollbar(
            summary,
            orient="vertical",
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        ttk.Label(
            summary,
            textvariable=self.summary_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        log_frame = ttk.LabelFrame(self.root, text="動作ログ", padding=8)
        log_frame.pack(fill="both", padx=12, pady=(0, 12))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            height=10,
            wrap="none",
            state="disabled",
            font=("Consolas", 9),
        )
        log_scroll_y = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        log_scroll_x = ttk.Scrollbar(
            log_frame,
            orient="horizontal",
            command=self.log_text.xview,
        )
        self.log_text.configure(
            yscrollcommand=log_scroll_y.set,
            xscrollcommand=log_scroll_x.set,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll_y.grid(row=0, column=1, sticky="ns")
        log_scroll_x.grid(row=1, column=0, sticky="ew")

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line.rstrip() + "\n")
        self.log_text.see("end")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 3000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.configure(state="disabled")

    def _set_running_ui(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.status_var.set("監視中" if running else "停止中")
        if not running and self.counter_window is not None:
            self.counter_window.set_stopped()

    def _load_config_and_counts(self) -> tuple[engine.AppConfig, dict[str, int]]:
        config = engine.load_config()
        counts = engine.load_counts(config.count_file)
        return config, counts

    def reload_configuration(self) -> None:
        if self.worker.running:
            messagebox.showinfo(
                "設定再読込",
                "監視を停止してから設定を再読込してください。",
                parent=self.root,
            )
            return

        try:
            config, counts = self._load_config_and_counts()
        except Exception as error:
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            self.summary_var.set(f"設定エラー: {error}")
            self._append_log(f"設定エラー: {error}")
            return

        selected = self.tree.selection()
        selected_name = selected[0] if selected else None
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)

        for rule in config.rules:
            self.tree.insert(
                "",
                "end",
                iid=rule.name,
                text=rule.name,
                values=(
                    "数字OCR" if rule.detector == "number" else "画像一致",
                    "カウント＋音" if rule.sound_enabled else "カウント",
                    "---",
                    "停止",
                    "OCR待機" if rule.detector == "number" else "PNG/JPEG画像一致",
                ),
            )

        if selected_name and self.tree.exists(selected_name):
            self.tree.selection_set(selected_name)

        sound_rules = sum(1 for rule in config.rules if rule.sound_enabled)
        self.summary_var.set(
            f"ルール {len(config.rules)}件／音通知 {sound_rules}件"
        )
        if self.counter_window is not None and self.counter_window.exists:
            self.counter_window.set_rules(config.rules, counts)

    def _missing_template_rules(self, config: engine.AppConfig) -> list[str]:
        missing: list[str] = []
        for rule in config.rules:
            if rule.detector != "template":
                continue
            path = rule.template_path
            if (
                path is None
                or path.suffix.lower() not in IMAGE_EXTENSIONS
                or not path.is_file()
            ):
                missing.append(rule.name)
        return missing

    def start_monitoring(self) -> None:
        if self.worker.running:
            return
        try:
            config = engine.load_config()
        except Exception as error:
            message = str(error)
            if "template" in message.lower() or "png/jpeg" in message.lower():
                open_now = messagebox.askyesno(
                    "画像が未登録です",
                    f"画像識別ルールの登録内容に問題があります。\n\n{message}\n\n"
                    "設定画面を開いてPNG/JPEGを登録しますか？",
                    parent=self.root,
                )
                if open_now:
                    self._show_settings(None, auto_select_region=False)
            else:
                messagebox.showerror(
                    "監視開始",
                    f"設定を読み込めません。\n\n{error}",
                    parent=self.root,
                )
            return

        missing = self._missing_template_rules(config)
        if missing:
            first = missing[0]
            names = "\n".join(f"・{name}" for name in missing)
            open_now = messagebox.askyesno(
                "画像が未登録です",
                "次の画像識別ルールにPNG/JPEGが登録されていません。\n\n"
                f"{names}\n\n設定画面を開いて登録しますか？",
                parent=self.root,
            )
            if open_now:
                self._show_settings(first, auto_select_region=False)
            return

        self._set_running_ui(True)
        self.summary_var.set("監視を開始しています。")
        self.worker.start()

    def stop_monitoring(self) -> None:
        if not self.worker.running:
            self._set_running_ui(False)
            return
        self.status_var.set("停止処理中")
        self.stop_button.configure(state="disabled")
        self.worker.stop()

    def _clear_when_stopped(self, rule_name: str | None) -> None:
        try:
            config = engine.load_config()
            counts = engine.load_counts(config.count_file)
            states = {
                rule.name: engine.RuleState(count=counts.get(rule.name, 0))
                for rule in config.rules
            }
            engine.clear_counts(config, states, rule_name=rule_name)
            self.reload_configuration()
        except Exception as error:
            messagebox.showerror("カウントクリア", str(error), parent=self.root)

    def clear_count_by_name(self, rule_name: str) -> None:
        try:
            config = engine.load_config()
            target = next((rule for rule in config.rules if rule.name == rule_name), None)
        except Exception as error:
            messagebox.showerror("カウントクリア", str(error), parent=self.root)
            return
        if target is None:
            messagebox.showinfo(
                "カウントクリア",
                "選択ルールが見つかりません。",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "カウントクリア",
            f"「{rule_name}」のカウントを0にしますか？",
            parent=self.counter_window.window if self.counter_window else self.root,
        ):
            return
        if self.worker.running:
            self.worker.request_clear(rule_name)
        else:
            self._clear_when_stopped(rule_name)

    def clear_all_counts(self) -> None:
        if not messagebox.askyesno(
            "全カウントクリア",
            "すべてのカウントを0にしますか？",
            parent=self.counter_window.window if self.counter_window else self.root,
        ):
            return
        if self.worker.running:
            self.worker.request_clear(None)
        else:
            self._clear_when_stopped(None)

    def _selected_rule_name(self) -> str | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return selection[0]

    def open_settings(self) -> None:
        self._request_settings(None, auto_select_region=False)

    def select_monitoring_region(self) -> None:
        rule_name = self._selected_rule_name()
        if not rule_name:
            messagebox.showinfo(
                "監視範囲",
                "監視一覧から対象ルールを選択してください。",
                parent=self.root,
            )
            return
        self._request_settings(rule_name, auto_select_region=True)

    def _request_settings(
        self,
        rule_name: str | None,
        auto_select_region: bool,
    ) -> None:
        if self.worker.running:
            self.pending_settings = (rule_name, auto_select_region)
            self.stop_monitoring()
            return
        self._show_settings(rule_name, auto_select_region)

    def _show_settings(
        self,
        rule_name: str | None = None,
        auto_select_region: bool = False,
    ) -> None:
        try:
            open_setup_window(
                self.root,
                engine.CONFIG_PATH,
                on_saved=self._settings_saved,
                initial_rule_name=rule_name,
                auto_select_region=auto_select_region,
            )
        except Exception:
            return
        self.reload_configuration()

    def _settings_saved(self) -> None:
        self._append_log("設定を保存しました。")

    def show_counter_window(self) -> None:
        if self.counter_window is not None and self.counter_window.exists:
            self.counter_window.focus()
            return
        self.counter_window = CounterWindow(
            self.root,
            clear_one=self.clear_count_by_name,
            clear_all=self.clear_all_counts,
            on_close=self._counter_closed,
        )
        try:
            config, counts = self._load_config_and_counts()
            self.counter_window.set_rules(config.rules, counts)
        except Exception as error:
            self._append_log(f"カウンター表示エラー: {error}")

    def _counter_closed(self) -> None:
        self.counter_window = None

    def open_evidence_folder(self) -> None:
        try:
            config = engine.load_config()
            path = config.evidence_dir
        except Exception:
            path = APP_DIR / "evidence"
        path.mkdir(parents=True, exist_ok=True)

        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                messagebox.showinfo(
                    "証跡画像",
                    str(path),
                    parent=self.root,
                )
        except OSError as error:
            messagebox.showerror("証跡画像", str(error), parent=self.root)

    def _update_status_rows(self, rows: list[dict[str, Any]]) -> None:
        active_count = 0
        for row in rows:
            name = str(row["name"])
            if not self.tree.exists(name):
                self.tree.insert("", "end", iid=name, text=name)
            detector = "数字OCR" if row["detector"] == "number" else "画像一致"
            action = "カウント＋音" if row.get("sound_enabled") else "カウント"
            state = "成立" if row["active"] else "監視中"
            if row["active"]:
                active_count += 1
            self.tree.item(
                name,
                text=name,
                values=(
                    detector,
                    action,
                    row["metric"],
                    state,
                    (
                        f"更新 {row.get('scan_time', '---')} "
                        f"#{row.get('scan_seq', 0)} / "
                        f"処理 {row.get('scan_ms', 0)}ms / "
                        f"{row.get('detail', '')}"
                    )[:120],
                ),
            )
        self.summary_var.set(
            f"監視ルール {len(rows)}件／条件成立 {active_count}件／各ルール独立監視"
        )
        if self.counter_window is not None and self.counter_window.exists:
            self.counter_window.update_rows(rows)

    def _update_counts(self, counts: dict[str, int]) -> None:
        if self.counter_window is not None and self.counter_window.exists:
            self.counter_window.update_counts(counts)

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event.kind == "log":
                    self._append_log(str(event.payload))
                elif event.kind == "started":
                    self._set_running_ui(True)
                    self.summary_var.set("監視中です。")
                elif event.kind == "status":
                    self._update_status_rows(event.payload)
                elif event.kind == "counts":
                    self._update_counts(event.payload)
                elif event.kind == "error":
                    payload = event.payload or {}
                    self._append_log(str(payload.get("detail", "")))
                    messagebox.showerror(
                        "監視エラー",
                        str(payload.get("message", "不明なエラー")),
                        parent=self.root,
                    )
                elif event.kind == "stopped":
                    self._set_running_ui(False)
                    self.reload_configuration()
                    if self.pending_settings is not None:
                        rule_name, auto_select = self.pending_settings
                        self.pending_settings = None
                        self.root.after(
                            50,
                            lambda n=rule_name, a=auto_select: self._show_settings(n, a),
                        )
                    if self.closing:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass

        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self.worker.running:
            self.status_var.set("終了処理中")
            self.worker.stop()
            self.root.after(5000, self._force_close_if_needed)
        else:
            self.root.destroy()

    def _force_close_if_needed(self) -> None:
        if self.root.winfo_exists() and self.closing:
            self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--region-selector-helper":
        if len(arguments) not in {3, 4}:
            return 2
        selector_options = arguments[3] if len(arguments) == 4 else None
        return run_region_selector_helper(
            arguments[1],
            arguments[2],
            selector_options,
        )

    set_dpi_awareness()
    root = tk.Tk()
    try:
        MainApplication(root)
        root.mainloop()
        return 0
    except Exception as error:
        try:
            messagebox.showerror(
                "ScreenImageMonitor",
                f"起動できませんでした。\n\n{error}",
                parent=root,
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
