# v2 修正点

- Tesseract OCR本体と英語数字認識データを`ScreenImageMonitor.exe`内部へ同梱します。
- 配布先に`tesseract`フォルダーを置く必要はありません。
- GitHub Actionsは統合GUIの`screen_image_monitor_gui.py`をビルドします。
- `--windowed`実行時に標準入力を使用しないため、`input(): lost sys.stdin`は発生しません。

# ScreenImageMonitor GUI / OCR版

Windows画面の指定領域を監視し、画像一致または数字OCRの条件に応じて音通知・カウントアップを行います。

## 主な機能

- GUIでモニターを選択し、スクリーンショット上をドラッグして監視領域を指定
- 数字OCRルールと画像一致ルールをGUIから追加
- 選択領域をその場でスクリーンショット保存
  - 数字OCR: `samples/<ルール名>.png`
  - 画像一致: `templates/<ルール名>.png`
- 取り込んだ画面でOCR結果または画像一致率をテスト
- カウント値をGUIで確認・クリア
- 条件成立時の証跡スクリーンショットを`evidence/<ルール名>/`へ保存
- 音通知とカウントアップをルールごとに分離
- 複数モニターと負の画面座標に対応

## GitHub ActionsでEXEを作成

1. ZIPの内容をGitHubリポジトリ直下へ配置します。
2. GitHubの`Actions`を開きます。
3. `Build Windows GUI OCR EXE`を選択します。
4. `Run workflow`を実行します。
5. 完了後、Artifactの`ScreenImageMonitor-GUI-Windows`をダウンロードします。

ビルドは`windows-latest`上で実行され、PyInstallerの単一EXE形式で生成されます。Tesseract OCR本体も配布フォルダーへ同梱されます。

## 初期設定

配布ZIPを展開し、次をダブルクリックします。

```text
OpenSetup.bat
```

GUIでは次の順で設定します。

1. 左側で既存ルールを選ぶか、新しいルールを追加
2. 対象モニターを選択
3. `画面からドラッグ選択`を押す
4. 表示されたスクリーンショット上で対象範囲をドラッグ
5. Enterで確定
6. `現在の選択領域を取り込む`を押す
7. `OCR／画像一致テスト`で確認
8. `ルールへ反映`
9. `config.jsonへ保存`

選択画面ではEscで取り消せます。

## ルールの種類

### 数字OCR＋カウント

画面に表示された数字が増加・減少・変化した場合や、指定値の条件を満たした場合にカウントアップします。

利用可能な条件:

```text
eq        指定値と一致
ne        指定値と不一致
gt        指定値より大きい
ge        指定値以上
lt        指定値より小さい
le        指定値以下
between   指定範囲内
changed   前回値から変化
increase  前回値から増加
decrease  前回値から減少
```

### 数字OCR＋音通知

数字が指定値以上などの条件を満たした際に音を鳴らします。同じ条件が継続している間は1回だけ動作し、条件解除後に再成立すると再度鳴ります。

### 画像一致＋カウント

GUIで選択した領域をテンプレート画像として保存し、現在画面がテンプレートと一致した際にカウントアップします。

### 画像一致＋音通知

GUIで取り込んだ画像と画面が一致した際に音を鳴らします。

## 監視開始

```text
StartMonitor.bat
```

または直接実行します。

```bat
ScreenImageMonitor.exe
```

監視中のキー操作:

```text
C       全カウントをクリア
Q       終了
Esc     終了
```

## カウントの確認とクリア

GUI設定画面では、選択したカウントルールの現在値を表示します。`このカウントをクリア`で選択ルールだけを0にできます。

全カウントクリア:

```bat
ClearCounts.bat
```

```bat
ScreenImageMonitor.exe --clear-counts
```

個別クリア:

```bat
ScreenImageMonitor.exe --clear-count "数値増加回数"
```

## スクリーンショット保存先

### 設定時のOCR確認画像

```text
samples/<ルール名>.png
```

### 画像一致用テンプレート

```text
templates/<ルール名>.png
```

### 動作時の証跡画像

```text
evidence/<ルール名>/YYYYMMDD_HHMMSS_count000001_value123.png
```

GUIの`動作時に証跡スクリーンショットを保存する`でルール単位に有効・無効を切り替えられます。

## OCR精度調整

GUIで領域をできるだけ数字だけに絞ってください。背景や単位記号を含めすぎると認識率が下がります。

必要に応じて`config.json`のOCR設定を調整できます。

```json
"ocr": {
  "psm": 7,
  "scale": 3.0,
  "threshold": "otsu",
  "invert": false,
  "whitelist": "0123456789.-",
  "timeout_seconds": 2.0,
  "number_index": 0,
  "border": 10
}
```

- `scale`: 小さい数字は3～5程度
- `threshold`: `otsu`、`adaptive`、`none`
- `invert`: 黒背景の白文字などで必要に応じて切替
- `psm`: 一列の数字は7、1文字だけなら10
- `whitelist`: 認識を許可する文字

## 注意事項

- Windowsの表示倍率変更後やモニター配置変更後はGUIで領域を再選択してください。
- 対象ウィンドウが最小化されている場合や、他のウィンドウに隠れている場合は、画面上に見えている内容を取得します。
- 画像一致ではアニメーション、時刻、カーソルなど変化する部分をテンプレートに含めないでください。
