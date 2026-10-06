# EDINET 株主優待記載取得アプリ（独立版）

既存の stock_app_backend とは別のアプリです。既存リポジトリへの上書きは不要です。
画面、サーバー、環境変数、データ保存を新しいRenderサービスで独立して管理します。
株テクその他の優待一覧を参照せず、EDINET公式APIと公式コードリストだけを使用します。

## 配置方法

1. このZIPを解凍します。
2. GitHubで新規リポジトリ（例：`edinet-benefits-app`）を作成します。
3. 解凍フォルダの中身を新規リポジトリの直下へアップロードします。
   `server.py`、`edinet_benefits.py`、`edinet_benefits.html` は必須です。
   フォルダごと入れた場合は、そのフォルダをRenderのRoot Directoryに設定してください。
4. Renderで New → Web Service を選択し、この新規リポジトリを指定します。
5. 以下の値を設定してデプロイします。

|設定|値|
|---|---|
|Language / Runtime|Python 3|
|Build Command|`pip install -r requirements.txt`|
|Start Command|`python server.py`|
|Health Check Path|`/health`|
|Environment|`EDINET_API_KEY` = 発行済みのキー|

既存サービスの環境変数は新サービスには自動でコピーされません。
新サービスにもキーを設定するか、RenderのEnvironment Groupを使って共有してください。
このコードにはAPIキーの値は含まれていません。
`render.yaml`を使うBlueprintでの作成にも対応しています。

6. 新サービスの `https://サービス名.onrender.com/` を開きます。
7. 「取得を開始」を押します。初回は直近400日を走査します。
8. 完了後に「優待JSONを保存」「確認用JSONを保存」を押します。

既存の `https://stock-app-api-ducat595.onrender.com` は更新しません。

## 動作

- 指定期間の書類一覧を日単位で取得（最大550日）。
- 証券コードのある提出者を対象に、その期間で最新の有価証券報告書と、
  半期・臨時・訂正報告書（120/130/160/170/180/190）を解析。
- 取下げ済・非開示、HTML/XBRLのない書類は除外。
- HTMLやXBRL本文の株主優待の記載を抽出。本文の数値を推測で補完しません。
- 業種はEDINETコードリストから補完。
- 廃止・未実施等が近傍に明示される記載は、候補JSONから除外して確認用JSONへ収録。
- APIキーやキーを含むリクエストURLを画面・JSON・ログに出力しません。
- バックグラウンド取得のため、HTTP要求の長時間待ちを避けています。
- 中断、進捗保存、保存データがある場合の再開、失敗分の再試行に対応。

## JSON形式と限界

添付形式の `updatedAt / dataStatus / notice / items` と同じ項目を出力します。
各レコードは `code, name, industry, month, recordDate, conditions, benefit,
 officialSource, confirmedAt, status, sourceType` です。

- 優待の**記載候補**です。国内全優待銘柄の最新マスターではありません。
- EDINET有価証券報告書に優待を記載しない企業もあります。
- PDFのみ・画像・解析サイズ上限を超える書類は対象外です。
- 未検出は優待がないことを意味しません。
- 複数の株数区分や時期の情報が原文抜粋に含まれます。
- 権利月を本文から判定できない場合は `month: null` です。
- `confirmedAt` は本文抽出日であり、最新制度の確認日ではありません。
- `officialSource` は企業IR未照合のため空欄。根拠のdocIDは `sourceType` に収録。
- 訂正報告書は部分的な記載なので、訂正前後の本文の人による照合が必要です。
- 最新の優待記載を持つ書類を銘柄ごとに採用します。後続書類で未検出でも
  優待廃止とは判断しません。廃止等の抽出も候補として確認が必要です。
- 一部失敗・処理途中で保存するJSONは取得済み分のみです。
- 変更・廃止・長期保有条件・最低株数は企業公式IRで確認してください。

## 進捗の保存

既定では `/tmp/edinet_benefits_cache/benefits/state.json` に保存します。
Renderの一時ファイルは再起動・再デプロイなどで消える場合があります。
無料プランの休止も長時間取得を中断する場合があります。
進捗が消えた場合は最初から取得してください。JSONを随時ダウンロードできます。
確実に再開したい場合は、永続ディスクのあるサービスを用意し、
`EDINET_BENEFITS_DIR` を永続ディスク配下（例：`/var/data/benefits`）へ設定してください。
単一プロセス・単一インスタンスで運用します。複数インスタンスの分散処理には未対応です。

## 任意設定

|環境変数|役割|
|---|---|
|`EDINET_BENEFITS_DIR`|進捗保存先|
|`EDINET_BENEFITS_ADMIN_TOKEN`|取得開始・再開・中断の管理トークン。画面の管理トークン欄に入力|
|`ALLOWED_ORIGINS`|独自ドメイン等の許可元URL、カンマ区切り|

Renderの標準公開URLは `RENDER_EXTERNAL_URL` から自動で許可します。
トークン未設定時は許可元チェックと取得数制限のみで、利用者認証はありません。
公開サービスで取得操作を管理者に限定する場合は任意トークンを設定してください。

## ローカル実行・テスト

Python 3.10以降で `python server.py` を実行し、`http://localhost:10000/` を開きます。
ローカル取得でも環境変数 `EDINET_API_KEY` が必要です。
`python -m unittest -v test_edinet_benefits.py` でモックデータによるテストを実行できます。
本環境ではRender側のキーにアクセスできないため、実キーでの全社取得は未検証です。

## API

- GET `/api/edinet/benefits/status`：進捗
- POST `/api/edinet/benefits/job`：`{"action":"start","startDate":"2025-09-03","endDate":"2026-10-07"}`
  または `{"action":"resume"}` / `{"action":"pause"}`
- GET `/api/edinet/benefits/export`：指定形式の優待候補JSON
- GET `/api/edinet/benefits/evidence`：原文抜粋・未検出・廃止等・失敗・解析対象外の記録

公式参照：
https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf
https://render.com/docs/environment-variables
https://render.com/docs/configure-environment-variables
