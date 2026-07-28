# クリエイティブ管理画面 実装計画

> **2026-07-28 方針更新**
> 人物・内装は架空生成せず、各サロンページ内のモデル／スタイル画像を使用する。対象画像は広告・編集利用が可能な前提とし、補正・1:1最適化を行い、店舗名・エリア・アクセス・訴求はコードで正確に合成する。CTAは表示しない。ネイル／アイ・ネイル商品では、爪が明瞭な完成写真を人物優先ルールの例外として扱える。
>
> **全件展開の最新決定**
> 標準は人物優先1:1エディトリアル版。一般ヘアの訴求は写真の人物を性別判定せず男女共通表現にし、店名・説明から専用店と確認できる `hair_mens` だけメンズ訴求を使う。マスク、顔なし、施術中、焼き込み文字を既定NGとし、背景の文字・ロゴは人物を保持したセグメンテーションで整理する。公開前に自動QA、人手事前チェック、バッチpreflightを必須にする。

- 目的: カタログサムネを一覧化し、**現在画像と承認済み人物優先1:1版を安全に切替**でき、切替後の配信結果まで振り返れる管理画面。
- 参照: 既存Hairbook Dashboardの `https://hairbook-dashboard.vercel.app/creative`。
- 現行実装: 既存認証付きの静的HTML＋Vercel Functions。全件進行、元人物画像との事前チェック、append-only承認、配信結果、実行・復元gateを実フィードとprivate Supabaseへ接続済み。Canvasのダミー生成やサンプル指標は使わない。
- 掲載画像の取得、選定、編集、QA、承認、配信の正本は、
  [`SALON_PAGE_PERSON_IMAGE_BANNER_PLAN.md`](SALON_PAGE_PERSON_IMAGE_BANNER_PLAN.md) とする。

---

## 1. 全体像（データの流れ）

```text
[管理画面 /creative]
  ├─ 画像設定: 現在画像＋人物優先1:1版の比較／個別調整／QA／承認／切替
  └─ 配信結果: 切替履歴／画像バージョン別KPI
        │
        ├─ private source image / source metadata / creative config / asset metadata
        ├─ creative_publish_events（不変の切替履歴）
        └─ 承認済み最終バナーだけpublic保存
                         │
                         ▼
[creative_batch.py] 承認済み完成バナーを限定更新 → [thumbnail_override] → [Meta]
                                                             │
                                                             ▼
                    [Meta product_id日次実績] → [creative_product_daily]
                                                             │
                    [creative_publish_events] ──時系列結合───┘
                                                             │
                                                             ▼
                                            [/creative 配信結果]
```

**要点**: 画像切替の正本はprivate設定、配信画像の正本はpublic JPG、結果紐付けの正本は `creative_publish_events`。Metaから画像URLは返らないため、切替履歴と `product_id × 日付` を結合して画像バージョン別実績を作る。

---

## 2. データモデル

### 一覧アイテム（GET用・読み取り）

既存データを結合して返す:
| 項目 | 出所 |
|---|---|
| id / image_link | フィード `入稿用データフィード_ローカル商品` |
| salon（既定） | フィード生成元の `salon_name`。`title` の逆解析はしない |
| area（既定） | `address.city`（例「大阪市中央区」） |
| verdict（情報判定） | `enrich` の `results.json`（ok/low_info/broken） |
| 現在画像 | フィードと `thumbnail_override` の有効URL |
| 人物優先1:1版 | 利用可能な掲載画像を加工したprivate asset metadataの最新承認可能版 |
| source画像 | private source metadata。元ページ、画像hash、source種別、人物種別、利用文脈 |
| source状態 | discovered／selected／quality_hold／stale |
| 現在の設定 | private `creative_config` |
| 配信結果 | `creative_product_daily` を画像バージョン別に集計 |

### creative_config（POST用・唯一の個別設定）

```json
{
  "44528_01KJ...": { "design": "bottom_scrim", "salon": "hair salon Lumière",
    "area": "東京都渋谷区・表参道", "badge": "", "enabled": true,
    "source": "edited_banner",
    "source_url": "https://raw.githubusercontent.com/.../<anonymous-hash>.jpg",
    "source_params": {"asset_id": "<anonymous id>", "candidate_slot": "model_image",
      "asset_version": 1, "representation": "salon_page_person_image_edit",
      "render_mode": "complete_banner"} },
  "44530_01KQ...": { "enabled": false }
}
```
- `design`: 新10種。`bottom_scrim`（標準）/ `caption` / `magazine` / `letterbox` / `namecard` / `tategaki` / `frameline` / `polaroid` / `poster` / `plate`。
- `source_url`: 管理画面で承認した**人物優先1:1完成バナー**のホスト先。
- `render_mode`: `complete_banner` は文字合成済みで帯を重ねない。`overlay` は編集済み背景へPillow側で帯を合成する。初期実験は1方式に固定する。
- 現在画像へ戻す場合は、公開前に保存した直前overrideを復元する。`source_url` を単純削除してautofix済み画像を失わない。
- 省略キーは既定にフォールバック（`enabled` 既定 true、`design` 既定 bottom_scrim）。

### 画像ソースのホスティング

- 標準の切替候補は「人物優先1:1版」1案。素材候補は管理画面で最大2枚から選び、ネイル／アイ・ネイル商品は完成した爪の写真を許可する。動画キャプチャ、疑似フレーム、架空人物・内装生成は扱わない。
- 編集元は対象サロンページまたは店舗掲載ページ内の利用可能なモデル／スタイル画像を優先し、画質・構図・訴求との一致で1枚を選定する。ヘア商品は顔主役を優先し、顔主役候補が1枚もない場合だけ、人物を含む完成ヘアの後ろ姿を限定例外にできる。
- 承認済み最終JPGだけを匿名ハッシュ名でpublic画像リポジトリへ保存し、`source_url` に格納する。
- 元モデル画像・元店内写真、元ページ、利用前提version、編集指示、候補履歴、承認者、切替履歴、配信実績はprivate側へ保存する。
- 再編集時は既存候補を上書きせず、同じ `candidate_slot` の `asset_version` を増やす。元人物を変更した場合はsource versionも増やし、再承認する。

### 画像切替履歴

`creative_publish_events` に、対象サロン、対象商品IDスナップショット、旧／新asset、候補番号、asset/design/copy version、公開時刻、フィード反映確認時刻、集計開始日、公開者、ロールバック理由を追記する。既存イベントは上書きしない。

### 配信実績

Metaの `product_id` 日次ブレイクダウンから spend / impressions / clicks / actions を取得し、`creative_publish_events` へ時系列結合して `creative_product_daily` を作る。切替日は旧新が混在し得るため除外し、最初の完全な配信日から集計する。

---

## 3. アーキテクチャ

### フロント（既存Dashboard `/creative`）

現行の構成:
- **全件進行**: 読込時点のin-stock product IDを分母に、現在画像、サロン／スタイリスト単位、進行、QA、承認、batch、overrideを表示。
- **事前チェック**: 元素材、1080×1080完成バナー、360×360プレビュー、元ページ、11項目、承認者、理由をunique asset単位で保存。
- **個別調整**: unique assetごとに素材候補、業種、エリア、店舗名、アクセス、訴求、文字配置、指摘種別を保存する。CTAは常時OFF。変更依頼はappend-onlyレビュー内の `checklist.edit_request` に保存し、対象assetだけを新manifestへ反映して再生成・再QA・再承認する。
- **配信結果**: 切替前28完全日、切替後7日・14日のCTR、CPM、商品別Meta ATCをサロン単位で比較。
- **実行・復元**: 全productが公開可能または理由・次回確認日付き保留になるまで本番操作を無効化。実処理はGitHub Actionsの承認environmentへ遷移。
- **絞り込み/検索**: 進行、サロン／スタイリスト、override、product ID、参照タイトル。

画像ソース欄は「現在画像」「人物優先1:1版」「承認済み素材候補1〜2枚」に限定する。旧プロトタイプのゼロベース生成説明は、サロン掲載画像の編集説明へ差し替える。

### プレビュー

- **正**: manifest駆動のPillow rendererが出力した1080×1080完成JPG。
- **確認**: 同じ完成JPGから作った360×360チェック画像。ブラウザCanvasによる近似合成は承認に使わない。

### 保存先

- private: 元モデル画像・元店内写真、source URL／hash、利用前提version、編集指示、`creative_config`、全候補、QA、承認、`creative_publish_events`、`creative_product_daily`。
- public: 承認済み最終JPGと匿名アセットIDだけ。
- repoコミット案を採用する場合も、public `hairbook-thumbnails` へprivateメタデータを置かない。

### API（Vercel Functions）

- `GET /api/creative-status` … 本番フィード、override、公開event、private rollout表、配信結果を結合。
- `POST /api/creative-review` … manifest hashに結び付くappend-onlyのassetレビューと構造化された個別調整を保存。
- 公開・復元APIはDashboardに持たせず、manifest SHAとbatch IDの再入力を要求するGitHub Actionsへ分離。

---

## 4. パイプライン側（現状と必要変更）

`thumbgen/enrich.py` は既に `creative_config.json` を読み、id別に
`design / salon / area / badge / enabled` を反映してエンリッチする（`enabled:false` は帯を付けない）。

`complete_banner` の承認・hash検証、二重合成防止、publisher、全量snapshot、append-only event、batch復元は実装・テスト済み。個別調整は `thumbgen/apply_person_square_edits.py` で新manifestへ反映する。旧manifestのレビューは新manifestへ引き継がず、必ず再承認する。現時点の本番画像変更は0件。

---

## 5. フェーズ

1. **利用前提・編集ルール**: 対象画像は利用可能という前提、選定基準、禁止編集を確定。
2. **source選定**: 8サロン程度でページ内のモデル／スタイル画像と店内写真を分類し、画質・構図・訴求からsourceを確定。
3. **人物優先1:1版を制作**: 掲載画像を補正・1:1最適化・コード合成し、OCR・マスク・施術中・CTA・アクセスを含む自動QAと人手承認を行う。ネイル／アイ・ネイルの完成写真例外以外で人物sourceがない場合は保留。
4. **UI更新（完了）**: 元素材・最終バナー・個別調整・全件進行・配信結果・実行復元へ更新。
5. **保存と公開基盤（完了）**: private metadata／承認保存、public最終JPG、publish event、限定publisher。
6. **実績取得基盤（完了）**: Meta `product_id` 日次実績＋商品別ATCを画像versionへ時系列結合。
7. **振り返りと運用**: 前後・holdout比較、source変更監視、段階ロールアウト、直前overrideへの即ロールバック。店内版を例外配信した場合だけ画像種別比較を追加。

---

## 6. 未確定・要決定

- 最小impressions・最低観測日数など、結果を参考値から判定可能へ切り替える基準。
- サロン名の専用値をフィード生成前データからどう渡すか（`title` は使わない）。
- holdoutを最終waveまで何%残すか。
- cajon-inc本体で `creative-production` environmentのrequired reviewerを誰にするか。

---

## 7. このリポジトリの関連物

- `dashboard/creative.html`／`creative.rollout.template.html`＋`build_dashboard.py`（認証付き実装の生成元）
- `dashboard/SALON_PAGE_PERSON_IMAGE_BANNER_PLAN.md`（人物画像の取得・選定・編集・QA・配信の正本）
- `thumbgen/enrich.py`（creative_config対応済みパイプライン）／ `overlays.py`（旧10デザイン）／ `improve.py`（低情報改善）
- `thumbgen/build_results_index.py`（結果一覧）／ `.github/workflows/thumbnail-enrich.yml`（定期実行）
