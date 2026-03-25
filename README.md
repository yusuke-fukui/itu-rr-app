# ITU-RR 条文参照グラフ v4

ITU無線通則（Radio Regulations）の条文間参照関係を双方向でトラバーサルできるStreamlitアプリ。
v4では**Vol.3 決議（Resolutions）ブラウザ**を追加。

## 経緯

### v1 → v3
v1ではRR全文をベクトル化しセマンティック検索。v2でツリー表示を追加。v3で**セマンティック検索を廃止し、`No. X.X` 明示参照のみの双方向リンク**に方針転換。条文参照グラフ（603条文、465リンク）を基盤に、ルート切替+パンくず履歴方式のUIを実装した。

### v4: Vol.3 決議ブラウザ追加
v3の全機能に加え、**Vol.3の191決議をPDFから直接パースし、条文⇔決議の双方向リンクを構築**する。

#### v4 新機能（予定）
- **Vol.3 決議ブラウザ**: 191決議をPDFダイレクトパースし構造化表示
- **決議⇔条文クロスリファレンス**: 決議内の`No. X.X`参照（447件）を抽出、双方向リンク
- **決議の階層表示**: RESOLUTION本文 → ANNEX → APPENDIX の構造可視化

---

## 主な機能

### 条文参照グラフ
- `No. X.X` 明示参照のみの双方向リンク（603条文、465リンク）
- **参照元**（この条文が参照している条文）を左カラム、**参照先**（この条文を参照している条文）を右カラムに表示
- →ボタンでルート切替、パンくず履歴 + 戻るボタン

### 手続きルートナビゲーション
- **非調整フロー**: 9.2B → 9.1 → 9.3 → ... → 11.31 → 11.41 → 11.44 → 11.47
- **調整フロー**: 9.30 → 9.1A → 9.35 → ... → 11.32 → 11.41 → 11.44 → 11.47
- サイドバーからルート開始、プログレスバーで進捗表示

### 注釈レイヤー
- **📘 Handbook Note**: Small Satellite Handbook 2023 の該当セクション解説
- **📜 Rules of Procedure**: RoP（2021 edition, rev.2）の該当セクション全文
- **📝 脚注**: RR条文の脚注を自動抽出・条文番号でフィルタリング

### AIサマリー
- 辿ったルート（2条文以上）をClaude Sonnetで整理
- サマリーキャッシュ（ハッシュキー方式）

---

## データ構造

```
data/graph/
├── reference_graph.json          # 双方向参照グラフ
├── articles.json                 # 条文テキスト（Vol.1 PDFから直接パース）
├── handbook_overlay.json         # Handbook条件ラベル・注釈・手続きルート定義
├── rop_sections.json             # RoP全セクション（PDFの見出し検出で抽出）
└── vol3_resolutions_draft.json   # Vol.3決議（191決議、ドラフト）
```

## セットアップ

```bash
pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-..." > .env
streamlit run src/app.py
```

## Streamlit Community Cloud デプロイ

1. [share.streamlit.io](https://share.streamlit.io) にGitHubアカウントでログイン
2. リポジトリ `yusuke-fukui/itu-rr-app` を選択
3. Main file path: `src/app.py`
4. Secrets に `ANTHROPIC_API_KEY` を設定
5. Deploy → 招待したユーザーのみアクセス可能
