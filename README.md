# v5.1 GitHub Actions build fix

GitHub Actions の `Verify source and imports` で、日本語文字列を `python -c` に直接渡して文字化けする問題を修正します。

上書き対象:

- `.github/workflows/build-windows.yml`
- `ui_separation_smoke_test.py`

修正後の Artifact 名:

- `ScreenImageMonitor-Windows-GUI-v5.1`
