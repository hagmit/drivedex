# drivedex

> **English summary** — drivedex signs in to SharePoint Online, OneDrive and Google Drive with OAuth (SSO) and keeps a **local, private index** of your files: instant search over hundreds of thousands of items, a change log (added / modified / deleted), a single-file HTML browser, and Office (docx / xlsx / pptx) to Markdown conversion for feeding documents to LLMs. Pure Python standard library, read-only, nothing is sent anywhere except the official Microsoft Graph / Google Drive APIs. MIT licensed.

SharePoint Online・OneDrive・Google ドライブのファイル構成を、**SSO でサインインして手元の PC に索引として持つ**ツールです。数十万件でも一瞬で検索でき、前回からの変更（追加・更新・削除）を一覧にできます。

- **Python 標準ライブラリだけで動きます**（pip で追加するものなし。Python 3.10 以上）
- **索引は利用者の PC の中だけ**（`%LOCALAPPDATA%\drivedex`）。ファイルの中身もメタデータも、外部サーバーへ送りません
- サインインは各社の正規の OAuth 2.0（認可コード＋PKCE、RFC 8252 のループバック方式）。パスワードは扱いません
- トークンは Windows の DPAPI で暗号化して保存します（そのWindowsユーザーでしか復号できない）
- 権限は**読み取りだけ**です

## できること

| コマンド | 内容 |
|---|---|
| `drivedex add ms --site <サイトURL>` / `--me` | SharePoint のドキュメントライブラリ／自分の OneDrive を登録 |
| `drivedex add google --my-drive` / `--shared-drive [名前]` | マイドライブ／共有ドライブを登録 |
| `drivedex sync [--full]` | 初回は全件、以後は**変更分だけ**取得（Graph の delta、Drive の changes）。変更の目印が失効していたら自動で全件を取り直す |
| `drivedex search <語> [<語>...]` | ファイル名で検索（スペース区切りで AND） |
| `drivedex changes [--days 7]` | 最近の追加・更新・削除 |
| `drivedex view` | 索引を**1枚の HTML** にして開く。検索・フォルダ移動・お気に入りをブラウザだけで（22万件で読み込み約0.2秒、検索約0.2〜0.3秒） |
| `drivedex get <ソース番号> <パス> [--md]` | ダウンロード。Google ドキュメント／スプレッドシート／スライドは docx／xlsx／pptx に変換。`--md` を付けると Markdown（`<元の名前>.md`）も作る |
| `drivedex md <ファイル>... [-o 出力先] [--stdout]` | 手元の docx／xlsx／pptx を Markdown に変換（AI に読ませる前の下ごしらえ） |

ビューアのキー操作：`j`/`k` 上下、`Enter` 開く、`u`/`Backspace` 上へ、`s` お気に入り、`/` 検索、`Esc` 検索解除。

### Markdown 変換で残すもの・捨てるもの

| 形式 | 残す | 捨てる |
|---|---|---|
| Word（docx） | 見出し（見出し1〜→`##`〜）、箇条書きと番号付き（階層つき）、表（横結合は空セル、縦結合は2行目以降を空）、リンク、テキストボックスの文字 | 書式、変更履歴の削除部分、ヘッダー・フッター、画像 |
| Excel（xlsx） | シートごとの表（空の行・列は詰める）、日付・時刻（`2026-11-11`、和暦や「yyyy年m月d日」も日付として扱う）、パーセント、改行は `<br>`、非表示シートの印 | ふりがな、数値の桁区切りや「円」などの表示書式、グラフ、画像。1シート5,000行まで（`--max-rows` で変更） |
| PowerPoint（pptx） | スライド番号とタイトル、本文の箇条書き（階層つき）、テキストボックス（上から、同じ高さなら左から）、表、ノート、非表示スライドの印 | ページ番号・日付・フッター、画像、図形の装飾 |

旧形式（doc／xls／ppt）とパスワード付きのファイルは対象外です。出力名は元の拡張子を残すので（`a.docx.md`、`a.xlsx.md`）、同じ名前のファイルが上書きし合いません。

## 使い始める

### 1. インストール

```
pip install drivedex
```

インストールしたくない場合は、ソースを取得したフォルダで `python -m drivedex ...` として使えます（依存パッケージはありません）。

```
git clone https://github.com/hagmit/drivedex
cd drivedex
python -m drivedex --help
```

### 2. アプリ登録（初回だけ）

利用する組織・個人が自分でアプリを登録し、その ID を設定します（製品として配布するときは、配布元が登録したアプリの ID を同梱する想定）。

**Microsoft（SharePoint / OneDrive）**

アプリの登録には Microsoft Entra のテナントが要ります。個人で試すときは Azure の無料アカウントを作るとテナントが付きます（登録時にカード情報を求められることがあります）。Microsoft 365 開発者プログラムの無料サンドボックスは、Visual Studio Professional/Enterprise の契約などが条件です。

1. Microsoft Entra 管理センター（https://entra.microsoft.com）→ アプリの登録 → 新規登録
2. 「サポートされているアカウントの種類」を選ぶ
   - 職場・学校のアカウントで SharePoint / OneDrive for Business を使う：「この組織ディレクトリのみ」または「任意の組織ディレクトリ」
   - 個人の Microsoft アカウントで個人用 OneDrive を使う：「個人用 Microsoft アカウントのみ」（または「任意の組織ディレクトリと個人用 Microsoft アカウント」）
3. 認証 → プラットフォームを追加 →「モバイル アプリケーションとデスクトップ アプリケーション」→ リダイレクト URI に `http://localhost`
4. 認証 →「パブリック クライアント フローを許可する」をはい
5. API のアクセス許可 → Microsoft Graph →「委任されたアクセス許可」：`User.Read`、`Files.Read.All`、`offline_access`、職場・学校で SharePoint も読むなら `Sites.Read.All`（**個人アカウントでは `Sites.Read.All` は使えない**ので、drivedex は `--tenant consumers` のとき要求しません）
6. 組織でユーザーの同意が止められている場合は、管理者の同意が必要です
7. 概要の「アプリケーション (クライアント) ID」を控える（シークレットは不要）

```
# 職場・学校
drivedex config ms --client-id <クライアントID> [--tenant <テナントID|organizations>]
# 個人の Microsoft アカウント
drivedex config ms --client-id <クライアントID> --tenant consumers
```

Microsoft（個人用 OneDrive）で試すときの一続きの手順：

```
cd drivedex   # ソースを取得したフォルダ（pip install した場合は不要）
python -m drivedex config ms --client-id <クライアントID> --tenant consumers
python -m drivedex login ms              # ブラウザで個人の Microsoft アカウントにサインインして許可
python -m drivedex add ms --me
python -m drivedex sync
python -m drivedex view
```

SharePoint（`add ms --site`）は職場・学校のテナントでしか確かめられません。

**Google（ドライブ）**
画面の名前は Google Cloud コンソールの「Google Auth Platform」（英語表示では Branding / Audience / Data Access / Clients）に合わせています。

1. https://console.cloud.google.com でプロジェクトを作る（例：drivedex-test）
2. API とサービス → ライブラリ →「Google Drive API」→ 有効にする
3. メニュー → Google Auth Platform →「開始」（OAuth 同意画面の作成）
   - アプリ情報：アプリ名（例：drivedex）とユーザーサポートのメール
   - 対象：個人の Gmail なら「外部」（「内部」は Google Workspace の組織だけ）
   - 連絡先のメールを入れ、ポリシーに同意して作成
4. 対象 → テストユーザー →「ユーザーを追加」で**自分の Google アカウントを追加**（公開ステータスが「テスト」のあいだ、追加していないアカウントはサインインを拒否される。テスト中の許可は7日で切れるので、切れたら `login google` をやり直す）
5. データアクセス →「スコープを追加または削除」→ 手動で `https://www.googleapis.com/auth/drive.readonly` を入れて追加 → 保存
6. クライアント →「クライアントを作成」→ アプリケーションの種類「デスクトップ アプリ」→ 名前を付けて作成 → 表示されたダイアログで **JSON をダウンロード**（クライアント シークレットは後から表示できない場合があるので、その場で保存する）

```
drivedex config google --client-secret-json <ダウンロードしたJSON>
```

Google で試すときの一続きの手順（インストールせずに動かす場合）：

```
cd drivedex   # ソースを取得したフォルダ（pip install した場合は不要）
python -m drivedex config google --client-secret-json <ダウンロードしたJSONのパス>
python -m drivedex login google          # ブラウザが開くのでサインインして許可
python -m drivedex add google --my-drive
python -m drivedex sync
python -m drivedex view                  # 索引の HTML が開く
```

サインインの画面で「このアプリは Google で確認されていません」と出たら、自分で作ったアプリなので「詳細」→「（アプリ名）に移動」で進めてよい。

### 3. サインインと取得

```
drivedex login ms
drivedex add ms --site https://contoso.sharepoint.com/sites/team
drivedex sync
drivedex view
```

設定・索引・トークンの置き場所は、通常は `%LOCALAPPDATA%\drivedex`、Microsoft Store 版の Python では `%USERPROFILE%\.drivedex` です（Store 版は AppData への書き込みを専用フォルダへ振り替えるため、ブラウザから HTML が見えなくなる）。`--home` か環境変数 `DRIVEDEX_HOME` で変えられます。

## 設計メモ

- 取得の流れ：プロバイダ（`providers/msgraph.py`、`providers/gdrive.py`）が Item／Deleted／Cursor を順に流し、`index.py` が1トランザクションで SQLite に反映します。途中で失敗したら丸ごと巻き戻すので、索引が半端な状態になりません
- Google の全件取得では、一覧を取る**前に**変更の目印（startPageToken）を取ります。一覧の最中に変わったファイルを取りこぼさないためです
- ダウンロードでリダイレクトされた先（署名付き URL）には、アクセストークンを送りません
- 親が索引に無いファイル（Google の「共有アイテム」など）は `(他の場所)` の下に置きます
- 初回の全件取得は「追加」として変更履歴に残しません（履歴が数十万件で埋まらないように）

## テスト

```
python -m unittest -v
```

API の応答を模したページで、索引の差分反映・巻き戻し・目印の失効時の全件取り直し・OAuth の往復（ループバックの受け口まで）・DPAPI の暗号化・HTML への安全な埋め込み・Office 変換を確かめます。

実機での確認状況：Google（個人アカウントのマイドライブ）でサインイン・全件取得・差分取得・削除の反映・ダウンロードを確認済み。Microsoft（SharePoint / OneDrive）は模擬テストのみです。

## ほかのツールとの使い分け

| やりたいこと | 向いているもの |
|---|---|
| クラウドとローカルのファイルを同期・コピーしたい | [rclone](https://rclone.org/) |
| いろいろな形式（PDF・画像など）を Markdown にしたい | [MarkItDown](https://github.com/microsoft/markitdown) |
| 何十万件のファイル構成を手元で一瞬で探したい、何が変わったかを知りたい、日本語の Excel（和暦の日付・ふりがな）をきれいに Markdown にしたい、pip が使えない PC で動かしたい | **drivedex** |

## 今後の予定（案）

- 複数アカウント・複数テナントの切り替え UI
- 変更の定期取得（タスク スケジューラ登録）と通知
- 配布用の署名付き実行ファイル、配布元のアプリ登録（Microsoft の発行元確認、Google の OAuth 審査）

## ライセンス

[MIT License](LICENSE)。不具合の報告や要望は GitHub の Issues へどうぞ。
