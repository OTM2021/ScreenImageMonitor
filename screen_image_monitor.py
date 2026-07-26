from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import cv2
import mss
import numpy as np
import pytesseract


ActionType = Literal["sound", "count"]
DetectorType = Literal["template", "number"]
NumberOperator = Literal[
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "between",
    "changed",
    "increase",
    "decrease",
]
ThresholdMode = Literal["none", "otsu", "adaptive"]


def application_directory() -> Path:
    """Return the directory containing the executable or source file."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = application_directory()
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "monitor.log"


@dataclass(frozen=True)
class ScreenRegion:
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class NumberCondition:
    operator: NumberOperator
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    tolerance: float = 0.0
    trigger_on_initial: bool = False


@dataclass(frozen=True)
class OcrOptions:
    psm: int = 7
    scale: float = 3.0
    threshold: ThresholdMode = "otsu"
    invert: bool = False
    whitelist: str = "0123456789.-"
    timeout_seconds: float = 2.0
    number_index: int = 0
    border: int = 10


@dataclass(frozen=True)
class Rule:
    name: str
    detector: DetectorType
    action: ActionType
    required_matches: int
    sound_path: Path | None = None

    # Template detector settings
    template_path: Path | None = None
    template_region: ScreenRegion | None = None
    match_threshold: float | None = None
    release_threshold: float | None = None

    # Numeric OCR detector settings
    number_region: ScreenRegion | None = None
    number_condition: NumberCondition | None = None
    ocr: OcrOptions | None = None

    # Evidence screenshot settings
    save_evidence: bool = False


@dataclass
class RuleState:
    consecutive_matches: int = 0
    target_is_present: bool = False
    count: int = 0

    # OCR stabilization and change detection
    pending_number: float | None = None
    pending_number_matches: int = 0
    stable_number: float | None = None
    last_action_number: float | None = None
    last_ocr_text: str = ""


@dataclass(frozen=True)
class AppConfig:
    monitor_region: ScreenRegion | None
    coordinate_mode: str
    check_interval_seconds: float
    show_status: bool
    count_file: Path
    evidence_dir: Path
    rules: list[Rule]


_LOG_LISTENERS: list[Callable[[str], None]] = []


def add_log_listener(listener: Callable[[str], None]) -> None:
    if listener not in _LOG_LISTENERS:
        _LOG_LISTENERS.append(listener)


def remove_log_listener(listener: Callable[[str], None]) -> None:
    try:
        _LOG_LISTENERS.remove(listener)
    except ValueError:
        pass


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"

    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except (OSError, AttributeError):
            pass

    try:
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass

    for listener in tuple(_LOG_LISTENERS):
        try:
            listener(line)
        except Exception:
            # A GUI/log listener must never stop monitoring.
            pass


def resolve_app_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else APP_DIR / path


def require_number(
    data: dict[str, Any],
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number.")

    result = float(value)
    if minimum is not None and result < minimum:
        raise ValueError(f"'{key}' must be at least {minimum}.")
    if maximum is not None and result > maximum:
        raise ValueError(f"'{key}' must be at most {maximum}.")
    return result


def optional_number(
    data: dict[str, Any],
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    default: float | None = None,
) -> float | None:
    if key not in data:
        return default
    return require_number(data, key, minimum=minimum, maximum=maximum)


def require_positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"'{key}' must be a positive integer.")
    return value


def optional_nonnegative_int(
    data: dict[str, Any], key: str, default: int
) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"'{key}' must be a non-negative integer.")
    return value


def parse_region(data: Any, context: str) -> ScreenRegion:
    if not isinstance(data, dict):
        raise ValueError(f"{context} must be an object.")

    return ScreenRegion(
        left=int(require_number(data, "left")),
        top=int(require_number(data, "top")),
        width=require_positive_int(data, "width"),
        height=require_positive_int(data, "height"),
    )


def parse_number_condition(data: Any, rule_name: str) -> NumberCondition:
    if not isinstance(data, dict):
        raise ValueError(f"Rule '{rule_name}': 'condition' must be an object.")

    operator = data.get("operator")
    allowed = {
        "eq",
        "ne",
        "gt",
        "ge",
        "lt",
        "le",
        "between",
        "changed",
        "increase",
        "decrease",
    }
    if operator not in allowed:
        raise ValueError(
            f"Rule '{rule_name}': unsupported numeric operator: {operator}"
        )

    tolerance = optional_number(data, "tolerance", minimum=0.0, default=0.0)
    assert tolerance is not None

    trigger_on_initial = data.get("trigger_on_initial", False)
    if not isinstance(trigger_on_initial, bool):
        raise ValueError(
            f"Rule '{rule_name}': 'trigger_on_initial' must be true or false."
        )

    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    if operator in {"eq", "ne", "gt", "ge", "lt", "le"}:
        value = optional_number(data, "value")
        if value is None:
            raise ValueError(
                f"Rule '{rule_name}': condition.value is required for '{operator}'."
            )

    if operator == "between":
        minimum = optional_number(data, "minimum")
        maximum = optional_number(data, "maximum")
        if minimum is None or maximum is None:
            raise ValueError(
                f"Rule '{rule_name}': minimum and maximum are required for between."
            )
        if minimum > maximum:
            raise ValueError(
                f"Rule '{rule_name}': minimum must not exceed maximum."
            )

    return NumberCondition(
        operator=operator,
        value=value,
        minimum=minimum,
        maximum=maximum,
        tolerance=tolerance,
        trigger_on_initial=trigger_on_initial,
    )


def parse_ocr_options(data: Any, rule_name: str) -> OcrOptions:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Rule '{rule_name}': 'ocr' must be an object.")

    psm = data.get("psm", 7)
    if not isinstance(psm, int) or isinstance(psm, bool) or not 0 <= psm <= 13:
        raise ValueError(f"Rule '{rule_name}': ocr.psm must be an integer 0-13.")

    scale = optional_number(data, "scale", minimum=1.0, maximum=10.0, default=3.0)
    timeout_seconds = optional_number(
        data,
        "timeout_seconds",
        minimum=0.1,
        maximum=30.0,
        default=2.0,
    )
    assert scale is not None and timeout_seconds is not None

    threshold = data.get("threshold", "otsu")
    if threshold not in {"none", "otsu", "adaptive"}:
        raise ValueError(
            f"Rule '{rule_name}': ocr.threshold must be none, otsu, or adaptive."
        )

    invert = data.get("invert", False)
    if not isinstance(invert, bool):
        raise ValueError(f"Rule '{rule_name}': ocr.invert must be true or false.")

    whitelist = data.get("whitelist", "0123456789.-")
    if not isinstance(whitelist, str) or not whitelist:
        raise ValueError(f"Rule '{rule_name}': ocr.whitelist must be non-empty.")

    number_index = optional_nonnegative_int(data, "number_index", 0)
    border = optional_nonnegative_int(data, "border", 10)

    return OcrOptions(
        psm=psm,
        scale=scale,
        threshold=threshold,
        invert=invert,
        whitelist=whitelist,
        timeout_seconds=timeout_seconds,
        number_index=number_index,
        border=border,
    )


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_PATH}")

    try:
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as file:
            raw = json.load(file)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"config.json is invalid near line {error.lineno}, column {error.colno}."
        ) from error

    if not isinstance(raw, dict):
        raise ValueError("The top level of config.json must be an object.")

    coordinate_mode = raw.get("coordinate_mode", "relative")
    if coordinate_mode not in {"relative", "absolute"}:
        raise ValueError("'coordinate_mode' must be 'relative' or 'absolute'.")

    monitor_data = raw.get("monitor")
    monitor_region: ScreenRegion | None = None
    if monitor_data is not None:
        monitor_region = parse_region(monitor_data, "'monitor'")
    elif coordinate_mode == "relative":
        raise ValueError("'monitor' is required when coordinate_mode is relative.")

    check_interval = require_number(
        raw,
        "check_interval_seconds",
        minimum=0.05,
    )

    show_status = raw.get("show_status", raw.get("show_preview", True))
    if not isinstance(show_status, bool):
        raise ValueError("'show_status' must be true or false.")

    count_file_value = raw.get("count_file", "counts.json")
    if not isinstance(count_file_value, str) or not count_file_value.strip():
        raise ValueError("'count_file' must be a non-empty string.")
    count_file = resolve_app_path(count_file_value)

    evidence_dir_value = raw.get("evidence_dir", "evidence")
    if not isinstance(evidence_dir_value, str) or not evidence_dir_value.strip():
        raise ValueError("'evidence_dir' must be a non-empty string.")
    evidence_dir = resolve_app_path(evidence_dir_value)

    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("'rules' must be a non-empty array.")

    rules: list[Rule] = []
    seen_names: set[str] = set()

    for index, item in enumerate(raw_rules, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Rule {index} must be an object.")

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Rule {index}: 'name' must be a non-empty string.")
        name = name.strip()

        if name in seen_names:
            raise ValueError(f"Rule name is duplicated: {name}")
        seen_names.add(name)

        action = item.get("action")
        if action not in ("sound", "count"):
            raise ValueError(
                f"Rule '{name}': 'action' must be 'sound' or 'count'."
            )

        detector = item.get("detector")
        if detector is None:
            detector = "template" if "template" in item else "number"
        if detector not in ("template", "number"):
            raise ValueError(
                f"Rule '{name}': 'detector' must be 'template' or 'number'."
            )

        required_matches = require_positive_int(item, "required_matches")

        sound_path: Path | None = None
        if action == "sound":
            sound_value = item.get("sound", "")
            if isinstance(sound_value, str) and sound_value.strip():
                sound_path = resolve_app_path(sound_value)

        save_evidence = item.get("save_evidence", action == "count")
        if not isinstance(save_evidence, bool):
            raise ValueError(
                f"Rule '{name}': 'save_evidence' must be true or false."
            )

        if detector == "template":
            template_value = item.get("template")
            if not isinstance(template_value, str) or not template_value.strip():
                raise ValueError(f"Rule '{name}': 'template' is required.")

            match_threshold = require_number(
                item,
                "match_threshold",
                minimum=0.0,
                maximum=1.0,
            )
            release_threshold = require_number(
                item,
                "release_threshold",
                minimum=0.0,
                maximum=1.0,
            )
            if release_threshold >= match_threshold:
                raise ValueError(
                    f"Rule '{name}': release_threshold must be lower than "
                    "match_threshold."
                )

            region_data = item.get("region")
            if region_data is None:
                if monitor_region is None:
                    raise ValueError(
                        f"Rule '{name}': 'region' is required in absolute mode."
                    )
                template_region = monitor_region
            else:
                template_region = parse_region(
                    region_data,
                    f"Rule '{name}': 'region'",
                )
                if coordinate_mode == "relative":
                    assert monitor_region is not None
                    template_region = ScreenRegion(
                        left=monitor_region.left + template_region.left,
                        top=monitor_region.top + template_region.top,
                        width=template_region.width,
                        height=template_region.height,
                    )

            rules.append(
                Rule(
                    name=name,
                    detector="template",
                    action=action,
                    required_matches=required_matches,
                    sound_path=sound_path,
                    template_path=resolve_app_path(template_value),
                    template_region=template_region,
                    match_threshold=match_threshold,
                    release_threshold=release_threshold,
                    save_evidence=save_evidence,
                )
            )
            continue

        number_region = parse_region(
            item.get("region"),
            f"Rule '{name}': 'region'",
        )
        if coordinate_mode == "relative":
            assert monitor_region is not None
            if (
                number_region.left < 0
                or number_region.top < 0
                or number_region.left + number_region.width > monitor_region.width
                or number_region.top + number_region.height > monitor_region.height
            ):
                raise ValueError(
                    f"Rule '{name}': number region must fit inside the monitor region."
                )
            number_region = ScreenRegion(
                left=monitor_region.left + number_region.left,
                top=monitor_region.top + number_region.top,
                width=number_region.width,
                height=number_region.height,
            )

        condition = parse_number_condition(item.get("condition"), name)
        ocr_options = parse_ocr_options(item.get("ocr"), name)

        rules.append(
            Rule(
                name=name,
                detector="number",
                action=action,
                required_matches=required_matches,
                sound_path=sound_path,
                number_region=number_region,
                number_condition=condition,
                ocr=ocr_options,
                save_evidence=save_evidence,
            )
        )

    return AppConfig(
        monitor_region=monitor_region,
        coordinate_mode=coordinate_mode,
        check_interval_seconds=check_interval,
        show_status=show_status,
        count_file=count_file,
        evidence_dir=evidence_dir,
        rules=rules,
    )


def load_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8-sig") as file:
            raw = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        log(f"Could not read count file; starting from zero: {error}")
        return {}

    if not isinstance(raw, dict):
        log("Count file has an invalid format; starting from zero.")
        return {}

    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }


def save_counts(path: Path, rules: list[Rule], states: dict[str, RuleState]) -> None:
    data = {
        rule.name: states[rule.name].count
        for rule in rules
        if rule.action == "count"
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary_path.replace(path)


def clear_counts(
    config: AppConfig,
    states: dict[str, RuleState],
    rule_name: str | None = None,
) -> None:
    count_rule_names = {
        rule.name for rule in config.rules if rule.action == "count"
    }

    if rule_name is not None:
        if rule_name not in count_rule_names:
            raise ValueError(
                f"Count rule not found: {rule_name}. Available count rules: "
                f"{', '.join(sorted(count_rule_names)) or '(none)'}"
            )
        target_names = {rule_name}
    else:
        target_names = count_rule_names

    for name in target_names:
        states[name].count = 0

    save_counts(config.count_file, config.rules, states)
    log("All counters were cleared." if rule_name is None else f"Counter '{rule_name}' was cleared.")


def configure_tesseract() -> Path:
    bundled = APP_DIR / "tesseract" / "tesseract.exe"
    configured: Path | None = None

    if bundled.exists():
        configured = bundled
    else:
        found = shutil.which("tesseract")
        if found:
            configured = Path(found)

    if configured is None:
        raise FileNotFoundError(
            "Tesseract OCR was not found. Keep the bundled 'tesseract' folder "
            "next to ScreenImageMonitor.exe, or install Tesseract and add it to PATH."
        )

    pytesseract.pytesseract.tesseract_cmd = str(configured)

    tessdata = configured.parent / "tessdata"
    if tessdata.exists():
        os.environ["TESSDATA_PREFIX"] = str(tessdata)

    return configured


def load_template(rule: Rule) -> np.ndarray:
    if rule.template_path is None:
        raise ValueError(f"Rule '{rule.name}': template path is missing.")
    if not rule.template_path.exists():
        raise FileNotFoundError(
            f"Rule '{rule.name}': template image not found: {rule.template_path}"
        )

    template = cv2.imread(str(rule.template_path), cv2.IMREAD_GRAYSCALE)
    if template is None or template.size == 0:
        raise ValueError(
            f"Rule '{rule.name}': template image could not be read: "
            f"{rule.template_path}"
        )
    return template


def calculate_template_match(
    screen_image: np.ndarray,
    template: np.ndarray,
) -> tuple[float, tuple[int, int], tuple[int, int]]:
    screen_height, screen_width = screen_image.shape[:2]
    template_height, template_width = template.shape[:2]

    if template_width > screen_width or template_height > screen_height:
        return 0.0, (0, 0), (template_width, template_height)

    # Uniform images can make correlation-based matching undefined.
    # Use normalized squared difference for those templates and invert the score
    # so that 1.0 always means a perfect match.
    if float(np.std(template)) < 1e-6:
        result = cv2.matchTemplate(
            screen_image,
            template,
            cv2.TM_SQDIFF_NORMED,
        )
        minimum_score, _, minimum_location, _ = cv2.minMaxLoc(result)
        score = 1.0 - float(minimum_score)
        location = minimum_location
    else:
        result = cv2.matchTemplate(
            screen_image,
            template,
            cv2.TM_CCOEFF_NORMED,
        )
        _, maximum_score, _, maximum_location = cv2.minMaxLoc(result)
        score = float(maximum_score)
        location = maximum_location

    if not math.isfinite(score):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return score, location, (template_width, template_height)


def crop_number_region(frame: np.ndarray, region: ScreenRegion) -> np.ndarray:
    x1 = region.left
    y1 = region.top
    x2 = x1 + region.width
    y2 = y1 + region.height
    return frame[y1:y2, x1:x2]


def preprocess_number_image(image: np.ndarray, options: OcrOptions) -> np.ndarray:
    if image.ndim == 3 and image.shape[2] == 4:
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    elif image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()

    if options.scale != 1.0:
        gray = cv2.resize(
            gray,
            None,
            fx=options.scale,
            fy=options.scale,
            interpolation=cv2.INTER_CUBIC,
        )

    if options.threshold == "otsu":
        _, gray = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY | cv2.THRESH_OTSU,
        )
    elif options.threshold == "adaptive":
        gray = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            11,
        )

    if options.invert:
        gray = cv2.bitwise_not(gray)

    if options.border > 0:
        gray = cv2.copyMakeBorder(
            gray,
            options.border,
            options.border,
            options.border,
            options.border,
            cv2.BORDER_CONSTANT,
            value=255,
        )

    return gray


NUMBER_PATTERN = re.compile(r"[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)")


def parse_numbers(text: str) -> list[float]:
    values: list[float] = []
    for match in NUMBER_PATTERN.findall(text):
        normalized = match.replace(",", ".")
        try:
            values.append(float(normalized))
        except ValueError:
            continue
    return values


def recognize_number(frame: np.ndarray, rule: Rule) -> tuple[float | None, str]:
    if rule.number_region is None or rule.ocr is None:
        raise ValueError(f"Rule '{rule.name}': OCR configuration is incomplete.")

    frame_height, frame_width = frame.shape[:2]
    if (
        frame_width == rule.number_region.width
        and frame_height == rule.number_region.height
    ):
        cropped = frame
    else:
        cropped = crop_number_region(frame, rule.number_region)
    processed = preprocess_number_image(cropped, rule.ocr)

    tessdata = Path(pytesseract.pytesseract.tesseract_cmd).parent / "tessdata"
    tessdata_argument = (
        f' --tessdata-dir "{tessdata}"' if tessdata.exists() else ""
    )
    config = (
        f"--oem 1 --psm {rule.ocr.psm}"
        f" -c tessedit_char_whitelist={rule.ocr.whitelist}"
        f"{tessdata_argument}"
    )

    try:
        text = pytesseract.image_to_string(
            processed,
            lang="eng",
            config=config,
            timeout=rule.ocr.timeout_seconds,
        ).strip()
    except RuntimeError as error:
        log(f"[{rule.name}] OCR timeout/error: {error}")
        return None, ""

    numbers = parse_numbers(text)
    if rule.ocr.number_index >= len(numbers):
        return None, text
    return numbers[rule.ocr.number_index], text


def numbers_equal(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance


def threshold_condition_matches(value: float, condition: NumberCondition) -> bool:
    tolerance = condition.tolerance
    if condition.operator == "eq":
        assert condition.value is not None
        return numbers_equal(value, condition.value, tolerance)
    if condition.operator == "ne":
        assert condition.value is not None
        return not numbers_equal(value, condition.value, tolerance)
    if condition.operator == "gt":
        assert condition.value is not None
        return value > condition.value + tolerance
    if condition.operator == "ge":
        assert condition.value is not None
        return value >= condition.value - tolerance
    if condition.operator == "lt":
        assert condition.value is not None
        return value < condition.value - tolerance
    if condition.operator == "le":
        assert condition.value is not None
        return value <= condition.value + tolerance
    if condition.operator == "between":
        assert condition.minimum is not None and condition.maximum is not None
        return (
            value >= condition.minimum - tolerance
            and value <= condition.maximum + tolerance
        )
    raise ValueError(
        f"Operator '{condition.operator}' is not a threshold condition."
    )


def play_sound(rule: Rule) -> None:
    if sys.platform != "win32":
        log(f"[{rule.name}] Sound is supported only on Windows.")
        return

    import winsound

    if rule.sound_path is not None and rule.sound_path.exists():
        winsound.PlaySound(
            str(rule.sound_path),
            winsound.SND_FILENAME | winsound.SND_ASYNC,
        )
    else:
        if rule.sound_path is not None:
            log(
                f"[{rule.name}] Sound file not found; using Windows default: "
                f"{rule.sound_path}"
            )
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def save_evidence_image(
    rule: Rule,
    state: RuleState,
    config: AppConfig,
    image: np.ndarray | None,
    detected_number: float | None,
) -> Path | None:
    if not rule.save_evidence or image is None or image.size == 0:
        return None

    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", rule.name).strip().strip(".") or "rule"
    rule_dir = config.evidence_dir / safe_name
    rule_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    count_suffix = f"_count{state.count:06d}" if rule.action == "count" else ""
    number_suffix = "" if detected_number is None else f"_value{detected_number:g}"
    path = rule_dir / f"{timestamp}{count_suffix}{number_suffix}.png"

    if image.ndim == 3 and image.shape[2] == 4:
        output = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    elif image.ndim == 3 and image.shape[2] == 3:
        output = image
    else:
        output = image

    if not cv2.imwrite(str(path), output):
        log(f"[{rule.name}] Could not save evidence screenshot: {path}")
        return None
    log(f"[{rule.name}] Evidence screenshot saved: {path}")
    return path


def execute_action(
    rule: Rule,
    state: RuleState,
    config: AppConfig,
    states: dict[str, RuleState],
    detected_number: float | None = None,
    evidence_image: np.ndarray | None = None,
) -> None:
    detail = "" if detected_number is None else f" Number={detected_number:g}."
    if rule.action == "sound":
        log(f"[{rule.name}] SOUND action executed.{detail}")
        save_evidence_image(rule, state, config, evidence_image, detected_number)
        play_sound(rule)
        return

    state.count += 1
    save_counts(config.count_file, config.rules, states)
    save_evidence_image(rule, state, config, evidence_image, detected_number)
    log(
        f"[{rule.name}] COUNT action executed. Current count: {state.count}.{detail}"
    )


def evaluate_template_rule(
    rule: Rule,
    state: RuleState,
    score: float,
    config: AppConfig,
    states: dict[str, RuleState],
    evidence_image: np.ndarray | None = None,
) -> None:
    assert rule.match_threshold is not None
    assert rule.release_threshold is not None

    if not state.target_is_present:
        if score >= rule.match_threshold:
            state.consecutive_matches += 1
        else:
            state.consecutive_matches = 0

        if state.consecutive_matches >= rule.required_matches:
            state.target_is_present = True
            state.consecutive_matches = 0
            log(f"[{rule.name}] Detected. Match score: {score:.3f}")
            execute_action(rule, state, config, states, evidence_image=evidence_image)
        return

    if score < rule.release_threshold:
        state.target_is_present = False
        state.consecutive_matches = 0
        log(f"[{rule.name}] Released; ready for the next detection.")


def update_stable_number(
    state: RuleState,
    value: float | None,
    required_matches: int,
    tolerance: float,
) -> bool:
    """Return True when a new value becomes stable in this iteration."""
    if value is None:
        state.pending_number = None
        state.pending_number_matches = 0
        return False

    if (
        state.pending_number is not None
        and numbers_equal(value, state.pending_number, tolerance)
    ):
        state.pending_number_matches += 1
    else:
        state.pending_number = value
        state.pending_number_matches = 1

    if state.pending_number_matches < required_matches:
        return False

    became_new_stable = (
        state.stable_number is None
        or not numbers_equal(value, state.stable_number, tolerance)
    )
    state.stable_number = value
    return became_new_stable


def evaluate_number_rule(
    rule: Rule,
    state: RuleState,
    number: float | None,
    ocr_text: str,
    config: AppConfig,
    states: dict[str, RuleState],
    evidence_image: np.ndarray | None = None,
) -> None:
    if rule.number_condition is None:
        raise ValueError(f"Rule '{rule.name}': number condition is missing.")

    condition = rule.number_condition
    state.last_ocr_text = ocr_text

    if condition.operator in {"changed", "increase", "decrease"}:
        became_stable = update_stable_number(
            state,
            number,
            rule.required_matches,
            condition.tolerance,
        )
        if not became_stable or state.stable_number is None:
            return

        current = state.stable_number
        previous = state.last_action_number

        if previous is None:
            state.last_action_number = current
            if condition.trigger_on_initial:
                log(f"[{rule.name}] Initial stable number detected: {current:g}")
                execute_action(rule, state, config, states, current, evidence_image)
            return

        should_trigger = False
        if condition.operator == "changed":
            should_trigger = not numbers_equal(current, previous, condition.tolerance)
        elif condition.operator == "increase":
            should_trigger = current > previous + condition.tolerance
        elif condition.operator == "decrease":
            should_trigger = current < previous - condition.tolerance

        # Always move the comparison baseline to the latest stable value.
        state.last_action_number = current
        if should_trigger:
            log(
                f"[{rule.name}] Numeric transition: {previous:g} -> {current:g}"
            )
            execute_action(rule, state, config, states, current, evidence_image)
        return

    matches = number is not None and threshold_condition_matches(number, condition)

    if not state.target_is_present:
        if matches:
            state.consecutive_matches += 1
        else:
            state.consecutive_matches = 0

        if state.consecutive_matches >= rule.required_matches:
            state.target_is_present = True
            state.consecutive_matches = 0
            assert number is not None
            log(f"[{rule.name}] Numeric condition matched: {number:g}")
            execute_action(rule, state, config, states, number, evidence_image)
        return

    if not matches:
        state.target_is_present = False
        state.consecutive_matches = 0
        log(f"[{rule.name}] Numeric condition released.")


def read_console_key() -> str | None:
    if sys.platform != "win32":
        return None

    import msvcrt

    if not msvcrt.kbhit():
        return None

    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):
        if msvcrt.kbhit():
            msvcrt.getwch()
        return None
    return key


def format_status(
    rows: list[tuple[Rule, RuleState, float | None, str]],
) -> str:
    parts: list[str] = []
    for rule, state, metric, raw_text in rows:
        if rule.detector == "template":
            status = "ACTIVE" if state.target_is_present else "WAIT"
            metric_text = "---" if metric is None else f"{metric:.3f}"
        else:
            status = "ACTIVE" if state.target_is_present else "WAIT"
            metric_text = "---" if metric is None else f"{metric:g}"
            if raw_text and metric is None:
                metric_text = f"OCR:{raw_text[:12]}"

        count_text = f"/count={state.count}" if rule.action == "count" else ""
        parts.append(f"{rule.name}:{metric_text}/{status}{count_text}")
    return " | ".join(parts)


def region_to_dict(region: ScreenRegion) -> dict[str, int]:
    return {
        "left": region.left,
        "top": region.top,
        "width": region.width,
        "height": region.height,
    }


def monitor(config: AppConfig) -> None:
    saved_counts = load_counts(config.count_file)
    templates: dict[str, np.ndarray] = {}
    states: dict[str, RuleState] = {}

    needs_ocr = any(rule.detector == "number" for rule in config.rules)
    if needs_ocr:
        tesseract_path = configure_tesseract()
        log(f"Tesseract OCR: {tesseract_path}")

    for rule in config.rules:
        if rule.detector == "template":
            templates[rule.name] = load_template(rule)
        states[rule.name] = RuleState(count=saved_counts.get(rule.name, 0))

    log("Screen monitoring started. Q/Esc exits; C clears all counters.")
    log(f"Coordinate mode: {config.coordinate_mode}")

    for rule in config.rules:
        if rule.detector == "template":
            assert rule.template_region is not None
            log(
                f"Rule '{rule.name}': detector=template, action={rule.action}, "
                f"region=({rule.template_region.left},{rule.template_region.top},"
                f"{rule.template_region.width},{rule.template_region.height}), "
                f"match={rule.match_threshold}, release={rule.release_threshold}, "
                f"required={rule.required_matches}, count={states[rule.name].count}"
            )
        else:
            assert rule.number_region is not None and rule.number_condition is not None
            log(
                f"Rule '{rule.name}': detector=number, action={rule.action}, "
                f"operator={rule.number_condition.operator}, "
                f"region=({rule.number_region.left},{rule.number_region.top},"
                f"{rule.number_region.width},{rule.number_region.height}), "
                f"required={rule.required_matches}, count={states[rule.name].count}"
            )

    with mss.mss() as capture:
        while True:
            status_rows: list[tuple[Rule, RuleState, float | None, str]] = []

            for rule in config.rules:
                state = states[rule.name]
                region = (
                    rule.template_region
                    if rule.detector == "template"
                    else rule.number_region
                )
                if region is None:
                    raise ValueError(f"Rule '{rule.name}': capture region is missing.")

                screenshot = capture.grab(region_to_dict(region))
                frame = np.asarray(screenshot)

                if rule.detector == "template":
                    grayscale_frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
                    score, _location, _size = calculate_template_match(
                        grayscale_frame,
                        templates[rule.name],
                    )
                    evaluate_template_rule(
                        rule,
                        state,
                        score,
                        config,
                        states,
                        evidence_image=frame,
                    )
                    status_rows.append((rule, state, score, ""))
                else:
                    number, raw_text = recognize_number(frame, rule)
                    evaluate_number_rule(
                        rule,
                        state,
                        number,
                        raw_text,
                        config,
                        states,
                        evidence_image=frame,
                    )
                    status_rows.append((rule, state, number, raw_text))

            key = read_console_key()
            if key in ("q", "Q", "\x1b"):
                log("Exit key received.")
                break
            if key in ("c", "C"):
                clear_counts(config, states)

            if config.show_status:
                print(
                    "\r" + format_status(status_rows)[:260].ljust(260),
                    end="",
                    flush=True,
                )

            time.sleep(config.check_interval_seconds)

    print()
    save_counts(config.count_file, config.rules, states)

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor screen images or OCR numbers and execute actions."
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open the GUI setup tool and exit.",
    )
    clear_group = parser.add_mutually_exclusive_group()
    clear_group.add_argument(
        "--clear-counts",
        action="store_true",
        help="Reset every count rule to zero, save, and exit.",
    )
    clear_group.add_argument(
        "--clear-count",
        metavar="RULE_NAME",
        help="Reset one named count rule to zero, save, and exit.",
    )
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_arguments()
        if args.setup:
            from screen_setup_gui import run_setup_gui

            run_setup_gui(CONFIG_PATH)
            return 0

        config = load_config()

        if args.clear_counts or args.clear_count is not None:
            saved_counts = load_counts(config.count_file)
            states = {
                rule.name: RuleState(count=saved_counts.get(rule.name, 0))
                for rule in config.rules
            }
            clear_counts(config, states, rule_name=args.clear_count)
            return 0

        monitor(config)
        return 0
    except KeyboardInterrupt:
        log("Monitoring stopped by keyboard interrupt.")
        return 0
    except Exception as error:
        log(f"ERROR: {error}")
        print()
        print("Correct config.json and required files, then start again.")
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
