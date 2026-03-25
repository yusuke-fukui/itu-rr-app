# ITU-RR 条文参照グラフ v3

ITU無線通則（Radio Regulations）の条文間参照関係を双方向でトラバーサルできるStreamlitアプリ。

## 経緯

### 当初の構想（v1 → v2）
v1ではRR全文をベクトル化し自然言語でセマンティック検索するアプリを構築。v2でツリー表示を追加したが、セマンティック検索ベースのツリーはスコアの揺らぎが大きく、手続きの「流れ」が見えにくかった。

### v3で方針転換
**セマンティック検索を廃止し、条文テキスト中の `No. X.X` 明示参照のみで双方向リンクを構築する**方針に転換。条文参照グラフ（603条文、465リンク）を基盤に、ルート切替+パンくず履歴方式のUIを実装。

### Small Satellite Handbook 統合
Handbook（2023年版）§3.5〜§3.10の手続き構造を `handbook_overlay.json` に整理し、条文ノードに**Handbook Note**として折りたたみ表示。非調整/調整の2つの手続きルートをナビゲーション可能にした。

### Rules of Procedure 統合
当初はチャンクベースでRoPを紐付けようとしたが、チャンカーが `No. X.X` 参照ごとに分割するため本文が断片化。**RoP PDFの四角囲み/太字セクション見出しを検出し、セクション間テキストを完全抽出する方式**に切り替えた（180セクション）。

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
- BIU（No. 11.44）はMaster Register記録後の運用フェーズに正しく配置

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
├── reference_graph.json   # 双方向参照グラフ
├── articles.json          # 条文テキスト（Vol.1 PDFから直接パース）
├── handbook_overlay.json  # Handbook条件ラベル・注釈・手続きルート定義
└── rop_sections.json      # RoP全セクション（PDFの見出し検出で抽出）
```

## セットアップ

```bash
pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-..." > .env
streamlit run src/app_v3.py
```

## Streamlit Community Cloud デプロイ

1. [share.streamlit.io](https://share.streamlit.io) にGitHubアカウントでログイン
2. リポジトリ `yusuke-fukui/itu-rr-app_v3` を選択（Private対応）
3. Main file path: `src/app_v3.py`
4. Secrets に `ANTHROPIC_API_KEY` を設定
5. Deploy → 招待したユーザーのみアクセス可能
