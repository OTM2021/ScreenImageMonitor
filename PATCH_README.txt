ScreenImageMonitor v6.6 修正パッチ

変更内容:
- 数値条件を日本語表示へ変更
- 「値 / 範囲」を条件別の1入力欄へ変更
- 範囲を 1-10 の形式で入力可能
- 「OCR結果に指定範囲の数値を含む」を追加
- OCR結果 121.1 を範囲 120-129 で検知可能
- 数字OCRテストで、読み取った数値と条件一致結果を表示

適用方法:
1. このZIPを展開します。
2. 中身をGitHubリポジトリ直下へ上書きアップロードします。
3. Commit changesを押します。
4. ActionsからBuild Windows Integrated GUI EXEを実行します。
5. Artifact「ScreenImageMonitor-Windows-GUI-v6.6」を取得します。

config.jsonとcounts.jsonはパッチに含まれていないため、既存設定とカウントは維持されます。
