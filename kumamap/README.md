# KumaMap data

仙台市の公式クマ出没オープンデータを、Androidアプリ向けJSONへ変換して公開するフォルダです。

- `incidents.json`: アプリが読み込む出没情報
- `status.json`: 更新日時と件数
- `update_data.py`: 仙台市CSVの取得・変換処理

GitHub Actionsは6時間ごと、および手動実行時に更新を確認します。
