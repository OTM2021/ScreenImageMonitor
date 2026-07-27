# ScreenImageMonitor v1.0 — Third-Party Software Notices

ScreenImageMonitor v1.0 uses or bundles open-source software listed below. Copyright remains with each project and its contributors. This file is an attribution summary; the original license texts collected during the build are included in `third_party_licenses/` and take precedence.

## Runtime and direct dependencies

| Component | Version used by this project | Purpose | License / copyright notice |
|---|---:|---|---|
| Python | 3.11 series | Application runtime and standard library | Python Software Foundation License Version 2. Copyright © Python Software Foundation and prior Python copyright holders. |
| NumPy | 1.26.4 | Image array and numeric processing | BSD 3-Clause. Copyright © 2005–2023 NumPy Developers. |
| opencv-python-headless | 4.10.0.84 | Python packaging and binary distribution of OpenCV | Packaging scripts: MIT, Copyright © Olli-Pekka Heinisuo. OpenCV binary: Apache License 2.0. The wheel also contains components under additional licenses; see its `LICENSE-3RD-PARTY.txt`. |
| OpenCV | 4.10.x | Template matching and image processing | Apache License 2.0. Copyright belongs to the OpenCV project and contributors. |
| MSS | 10.2.0 | Windows screen capture | MIT License. Author/maintainer: Mickaël Schoentgen and contributors. |
| pytesseract | 0.3.13 | Python wrapper that invokes Tesseract OCR | Apache License 2.0. Originally written by Samuel Hoffstaetter; copyright belongs to the project contributors. |
| Pillow | 10.x–12.x (`>=10,<13`) | GUI image preview and image conversion | MIT-CMU License. PIL copyright © Secret Labs AB, Fredrik Lundh and contributors; Pillow copyright © Jeffrey “Alex” Clark and contributors. |
| Tesseract OCR | Version installed by the GitHub Actions Chocolatey package at build time | OCR engine embedded in the EXE | Apache License 2.0. Tesseract uses Leptonica under a BSD 2-Clause-style license and may depend on other separately licensed libraries. |
| Tesseract English trained data | Build-time packaged `eng.traineddata` | English/numeric OCR model | Apache License 2.0. |
| Tcl/Tk / tkinter | Version bundled with the selected Python 3.11 runtime | GUI toolkit | Tcl/Tk license and licenses of components incorporated into the Python distribution. |

## Build-time dependency

| Component | Version | Use | License |
|---|---:|---|---|
| PyInstaller | 6.21.0 | Creates the standalone Windows EXE | GPL-2.0-or-later with the PyInstaller Bootloader Exception; selected files are Apache-2.0. The exception permits distribution of executables produced by PyInstaller under another license, subject to the licenses of bundled dependencies. |

GitHub Actions and the official `actions/checkout`, `actions/setup-python`, and `actions/upload-artifact` actions are used only in the build service. They are not shipped as part of `ScreenImageMonitor.exe`.

## OpenCV binary notice

The `opencv-python-headless` wheel includes OpenCV and additional binaries. Its upstream project states that the packaging scripts are MIT licensed, OpenCV itself is Apache-2.0 licensed, and wheels include FFmpeg under LGPL-2.1 along with other components identified in `LICENSE-3RD-PARTY.txt`. The exact third-party license file from the installed wheel is copied into `third_party_licenses/` during the build.

## Tesseract binary notice

The workflow installs a Windows Tesseract distribution and embeds its directory into the one-file EXE. Tesseract itself and the included trained data are Apache-2.0 licensed. The distribution can also contain Leptonica and image/Unicode/compression libraries under separate licenses. License, notice, copyright, and copying files found in the installed Tesseract directory are copied into `third_party_licenses/Tesseract-OCR/` during the build.

## Font notice

ScreenImageMonitor does not redistribute font files. The counter displays fonts already installed in Windows, such as `Segoe UI Light`, `Segoe UI`, or `Arial`. Rights in those fonts remain with their respective owners.

## No endorsement or affiliation

The names Python, NumPy, OpenCV, Tesseract, Pillow, PyInstaller, MSS, Tcl/Tk, and other project names are used only to identify the software components. Their inclusion does not imply endorsement of ScreenImageMonitor by the projects or copyright holders.

## Warranty and precedence

The open-source components are provided under their respective licenses and generally on an “AS IS” basis, without warranties. If this summary conflicts with an original license file, the original license file controls.

## Upstream references

- Python: <https://docs.python.org/3.11/license.html>
- NumPy: <https://github.com/numpy/numpy>
- OpenCV: <https://github.com/opencv/opencv>
- opencv-python: <https://github.com/opencv/opencv-python>
- MSS: <https://github.com/BoboTiG/python-mss>
- pytesseract: <https://github.com/madmaze/pytesseract>
- Pillow: <https://github.com/python-pillow/Pillow>
- Tesseract OCR: <https://github.com/tesseract-ocr/tesseract>
- Tesseract trained data: <https://github.com/tesseract-ocr/tessdata>
- PyInstaller: <https://pyinstaller.org/en/stable/license.html>
