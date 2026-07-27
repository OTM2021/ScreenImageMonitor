ScreenImageMonitor v6.6.1 完全修正パッチ

このパッチは、v6.6パッチで不足していた以下の必須ファイルを含みます。
- image_file_io.py
- unicode_image_io_smoke_test.py

また、GitHub ActionsのWorkflowが参照するPythonファイルをすべて同梱しています。
そのため、過去のパッチ適用状況に関係なく、ソース／検査ファイルをまとめて揃えられます。

適用方法:
1. ZIPを展開します。
2. 展開した中身をGitHubリポジトリ直下へまとめて上書きアップロードします。
3. Commit changesを押します。
4. Actions → Build Windows Integrated GUI EXE → Run workflow を実行します。
5. Artifact「ScreenImageMonitor-Windows-GUI-v6.6.1」をダウンロードします。

このパッチには config.json、counts.json、templates、sounds、samples、evidence を含めていません。
既存のルール設定、カウント、登録画像、音声ファイルは上書きされません。
