from __future__ import annotations

import importlib.metadata as metadata
import os
import re
import shutil
import sys
from pathlib import Path

OUTPUT_DIR = Path("third_party_licenses")
PACKAGE_NAMES = (
    "numpy",
    "opencv-python-headless",
    "mss",
    "pytesseract",
    "Pillow",
    "pyinstaller",
)
LICENSE_NAME_RE = re.compile(r"^(?:license|licence|copying|notice|copyright)(?:[._-]|$)", re.IGNORECASE)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "item"


def copy_file(source: Path, destination: Path) -> bool:
    try:
        if not source.is_file():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return True
    except OSError as exc:
        print(f"warning: could not copy {source}: {exc}")
        return False


def collect_distribution(name: str, version_lines: list[str]) -> None:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        print(f"warning: distribution not installed: {name}")
        return

    display_name = distribution.metadata.get("Name", name)
    version = distribution.version
    version_lines.append(f"{display_name}=={version}")
    destination_root = OUTPUT_DIR / safe_name(f"{display_name}-{version}")

    copied: set[str] = set()
    for file_entry in distribution.files or ():
        relative = Path(str(file_entry))
        in_license_directory = any(part.lower() in {"license", "licenses"} for part in relative.parts[:-1])
        if not LICENSE_NAME_RE.search(relative.name) and not in_license_directory:
            continue
        source = Path(distribution.locate_file(file_entry))
        destination = destination_root / relative
        key = str(destination).lower()
        if key in copied:
            continue
        if copy_file(source, destination):
            copied.add(key)

    # opencv-python stores the most important notices under cv2/ as well as dist-info.
    if display_name.lower().startswith("opencv-python"):
        package_root = Path(distribution.locate_file("cv2"))
        for candidate in (package_root / "LICENSE.txt", package_root / "LICENSE-3RD-PARTY.txt"):
            destination = destination_root / candidate.name
            key = str(destination).lower()
            if key not in copied and copy_file(candidate, destination):
                copied.add(key)

    if not copied:
        print(f"warning: no license files found for {display_name} {version}")


def collect_python_license(version_lines: list[str]) -> None:
    version_lines.append(f"Python=={sys.version.split()[0]}")
    candidates = (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.prefix) / "LICENSE.txt",
        Path(sys.executable).resolve().parent / "LICENSE.txt",
        Path(sys.executable).resolve().parent.parent / "LICENSE.txt",
    )
    for candidate in candidates:
        if copy_file(candidate, OUTPUT_DIR / "Python" / "LICENSE.txt"):
            return
    print("warning: Python LICENSE.txt was not found in the runner installation")


def collect_tesseract(version_lines: list[str]) -> None:
    raw_directory = os.environ.get("TESSERACT_DIR", "").strip()
    if not raw_directory:
        print("warning: TESSERACT_DIR is not set")
        return

    root = Path(raw_directory)
    executable = root / "tesseract.exe"
    version = "unknown"
    if executable.is_file():
        try:
            import subprocess

            output = subprocess.run(
                [str(executable), "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.splitlines()
            if output:
                version = output[0].strip()
        except OSError as exc:
            print(f"warning: could not query Tesseract version: {exc}")
    version_lines.append(f"Tesseract OCR=={version}")

    destination_root = OUTPUT_DIR / "Tesseract-OCR"
    copied = 0
    for candidate in root.rglob("*"):
        if candidate.is_file() and LICENSE_NAME_RE.search(candidate.name):
            relative = candidate.relative_to(root)
            if copy_file(candidate, destination_root / relative):
                copied += 1

    if copied == 0:
        print("warning: no Tesseract license files found in installed package")


def main() -> int:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    versions: list[str] = []
    collect_python_license(versions)
    for package_name in PACKAGE_NAMES:
        collect_distribution(package_name, versions)
    collect_tesseract(versions)

    (OUTPUT_DIR / "VERSIONS.txt").write_text(
        "Build-time component versions\n" + "\n".join(sorted(versions, key=str.lower)) + "\n",
        encoding="utf-8",
    )
    (OUTPUT_DIR / "README.txt").write_text(
        "This directory contains license, notice, and copyright files collected "
        "from the exact packages installed during the GitHub Actions build.\n"
        "The original files and the upstream license terms take precedence over "
        "the summaries in README.md and THIRD_PARTY_NOTICES.md.\n",
        encoding="utf-8",
    )

    files = [path for path in OUTPUT_DIR.rglob("*") if path.is_file()]
    print(f"Collected {len(files)} third-party notice files.")
    for path in sorted(files):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
