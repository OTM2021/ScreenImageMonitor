ScreenImageMonitor v6.3 範囲選択修正パッチ

このZIPを展開し、GitHubリポジトリ直下へ上書きアップロードしてください。
config.jsonとcounts.jsonは含めていないため、既存設定とカウントは維持されます。

主な変更:
- 「画面から範囲選択」へ名称統一
- 範囲選択画面を独立プロセスで起動
- 設定画面のmodal/grab状態に依存しない表示方式
- GitHub Actionsに範囲選択ヘルパー検査を追加

再ビルド後のArtifact:
ScreenImageMonitor-Windows-GUI-v6.3
