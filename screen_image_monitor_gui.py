from __future__ import annotations

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
from screen_setup_gui import open_setup_window, set_dpi_awareness


APP_DIR = engine.APP_DIR
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    payload: Any = None


class MonitorWorker:
    """Run the monitoring engine without blocking the Tk event loop."""

    def __init__(self, event_queue: queue.Queue[WorkerEvent]) -> None:
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self.command_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="ScreenImageMonitorWorker",
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
                engine.clear_counts(config, states, rule_name=value)
                self._put(
                    "counts",
                    {name: state.count for name, state in states.items()},
                )

    def _run(self) -> None:
        engine.add_log_listener(self._log_listener)
        states: dict[str, engine.RuleState] = {}

        try:
            config = engine.load_config()
            saved_counts = engine.load_counts(config.count_file)
            templates: dict[str, np.ndarray] = {}

            if any(rule.detector == "number" for rule in config.rules):
                tesseract_path = engine.configure_tesseract()
                engine.log(f"Tesseract OCR: {tesseract_path}")

            for rule in config.rules:
                if rule.detector == "template":
                    templates[rule.name] = engine.load_template(rule)
                states[rule.name] = engine.RuleState(
                    count=saved_counts.get(rule.name, 0)
                )

            self._put(
                "started",
                {
                    "rules": [
                        {
                            "name": rule.name,
                            "detector": rule.detector,
                            "action": rule.action,
                            "count": states[rule.name].count,
                        }
                        for rule in config.rules
                    ]
                },
            )
            engine.log("GUI monitoring started.")

            next_status_time = 0.0
            with mss.mss() as capture:
                while not self.stop_event.is_set():
                    self._process_commands(config, states)
                    status_rows: list[dict[str, Any]] = []

                    for rule in config.rules:
                        if self.stop_event.is_set():
                            break

                        state = states[rule.name]
                        region = (
                            rule.template_region
                            if rule.detector == "template"
                            else rule.number_region
                        )
                        if region is None:
                            raise ValueError(
                                f"ルール「{rule.name}」の監視領域が未設定です。"
                            )

                        screenshot = capture.grab(engine.region_to_dict(region))
                        frame = np.asarray(screenshot)

                        metric: str
                        active: bool
                        detail = ""
                        if rule.detector == "template":
                            gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
                            score, _location, _size = engine.calculate_template_match(
                                gray,
                                templates[rule.name],
                            )
                            engine.evaluate_template_rule(
                                rule,
                                state,
                                score,
                                config,
                                states,
                                evidence_image=frame,
                            )
                            metric = f"{score:.3f}"
                            active = state.target_is_present
                            template_name = (
                                rule.template_path.name
                                if rule.template_path is not None
                                else "未登録"
                            )
                            detail = f"PNG/JPEG一致: {template_name}"
                        else:
                            number, raw_text = engine.recognize_number(frame, rule)
                            engine.evaluate_number_rule(
                                rule,
                                state,
                                number,
                                raw_text,
                                config,
                                states,
                                evidence_image=frame,
                            )
                            metric = "---" if number is None else f"{number:g}"
                            active = state.target_is_present
                            detail = f"OCR: {raw_text}" if raw_text else "OCR待機"

                        status_rows.append(
                            {
                                "name": rule.name,
                                "detector": rule.detector,
                                "action": rule.action,
                                "metric": metric,
                                "detail": detail,
                                "active": active,
                                "count": state.count,
                            }
                        )

                    now = time.monotonic()
                    if now >= next_status_time:
                        self._put("status", status_rows)
                        next_status_time = now + 0.25

                    self.stop_event.wait(config.check_interval_seconds)

            engine.save_counts(config.count_file, config.rules, states)
            engine.log("GUI monitoring stopped.")

        except Exception as error:
            detail = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            engine.log(f"ERROR: {error}")
            self._put("error", {"message": str(error), "detail": detail})
        finally:
            engine.remove_log_listener(self._log_listener)
            self.thread = None
            self._put("stopped")


class CounterWindow:
    """Independent counter display window."""

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
        self.window = tk.Toplevel(parent)
        self.window.title("ScreenImageMonitor カウンター")
        self.window.geometry("560x430")
        self.window.minsize(430, 260)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        outer = ttk.Frame(self.window, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text="カウンター",
            font=("Yu Gothic UI", 18, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.tree = ttk.Treeview(
            outer,
            columns=("count", "state"),
            show="tree headings",
            selectmode="browse",
            style="Counter.Treeview",
        )
        self.tree.heading("#0", text="ルール名")
        self.tree.heading("count", text="回数")
        self.tree.heading("state", text="状態")
        self.tree.column("#0", width=300, minwidth=180)
        self.tree.column("count", width=110, anchor="e", stretch=False)
        self.tree.column("state", width=90, anchor="center", stretch=False)
        self.tree.grid(row=1, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")

        buttons = ttk.Frame(outer)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(
            buttons,
            text="選択カウントをクリア",
            command=self._clear_selected,
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="全カウントをクリア",
            command=self.clear_all_callback,
        ).pack(side="left", padx=(8, 0))
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
        if self.exists:
            self.window.destroy()
        self.on_close_callback()

    def _clear_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(
                "カウントクリア",
                "クリアするカウントを選択してください。",
                parent=self.window,
            )
            return
        self.clear_one_callback(selection[0])

    def set_rules(self, rules: list[engine.Rule], counts: dict[str, int]) -> None:
        selected = self.tree.selection()
        selected_name = selected[0] if selected else None
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)

        for rule in rules:
            if rule.action != "count":
                continue
            self.tree.insert(
                "",
                "end",
                iid=rule.name,
                text=rule.name,
                values=(counts.get(rule.name, 0), "停止"),
            )

        if selected_name and self.tree.exists(selected_name):
            self.tree.selection_set(selected_name)

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if row.get("action") != "count":
                continue
            name = str(row["name"])
            if not self.tree.exists(name):
                self.tree.insert("", "end", iid=name, text=name)
            self.tree.item(
                name,
                text=name,
                values=(row.get("count", 0), "成立" if row.get("active") else "監視中"),
            )

    def update_counts(self, counts: dict[str, int]) -> None:
        for name, value in counts.items():
            if not self.tree.exists(name):
                continue
            current = list(self.tree.item(name, "values"))
            while len(current) < 2:
                current.append("停止")
            current[0] = value
            self.tree.item(name, values=current)

    def set_stopped(self) -> None:
        for name in self.tree.get_children():
            current = list(self.tree.item(name, "values"))
            while len(current) < 2:
                current.append("")
            current[1] = "停止"
            self.tree.item(name, values=current)


class MainApplication:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ScreenImageMonitor")
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

        counter_font = tkfont.Font(
            self.root,
            family="Yu Gothic UI",
            size=15,
            weight="bold",
        )
        style.configure(
            "Counter.Treeview",
            font=counter_font,
            rowheight=counter_font.metrics("linespace") + 14,
        )
        style.configure(
            "Counter.Treeview.Heading",
            font=("Yu Gothic UI", 11, "bold"),
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
            text="選択ルールの監視範囲を指定",
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

        count_rules = 0
        for rule in config.rules:
            if rule.action == "count":
                count_rules += 1
            self.tree.insert(
                "",
                "end",
                iid=rule.name,
                text=rule.name,
                values=(
                    "数字OCR" if rule.detector == "number" else "画像一致",
                    "カウント" if rule.action == "count" else "音通知",
                    "---",
                    "停止",
                    "OCR待機" if rule.detector == "number" else "PNG/JPEG画像一致",
                ),
            )

        if selected_name and self.tree.exists(selected_name):
            self.tree.selection_set(selected_name)

        self.summary_var.set(
            f"ルール {len(config.rules)}件／カウント対象 {count_rules}件"
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
        if target is None or target.action != "count":
            messagebox.showinfo(
                "カウントクリア",
                "選択ルールはカウント対象ではありません。",
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
            action = "カウント" if row["action"] == "count" else "音通知"
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
                    str(row.get("detail", ""))[:80],
                ),
            )
        self.summary_var.set(
            f"監視ルール {len(rows)}件／条件成立 {active_count}件"
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


def main() -> int:
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
