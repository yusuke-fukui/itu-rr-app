"""
ITU-RR 条文検索 Streamlitアプリ。
自然言語でITU無線通則の条文を検索し、Claude Haikuによる日本語解説を表示する。
インタラクティブ関連条文ツリー、ブックマーク、検索履歴機能を搭載。
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

# プロジェクトルートを基準にパスを設定
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

# 同じディレクトリのモジュールをインポート
from searcher import RRSearcher
from tree_engine import expand_node, find_article_text, build_tree_markdown
from storage import get_storage

# --- ページ設定 ---
st.set_page_config(
    page_title="ITU-RR 条文検索",
    page_icon="📡",
    layout="wide",
)

# --- ストレージ ---
storage = get_storage()


# --- ブックマーク・検索履歴の読み込み ---
def load_bookmarks() -> list:
    """ブックマークを読み込む。"""
    data = storage.load("bookmarks")
    return data if isinstance(data, list) else []


def save_bookmarks(bookmarks: list):
    """ブックマークを保存する。"""
    storage.save("bookmarks", bookmarks)


def load_search_history() -> list:
    """検索履歴を読み込む。"""
    data = storage.load("search_history")
    return data if isinstance(data, list) else []


def save_search_history(history: list):
    """検索履歴を保存する（最大100件）。"""
    storage.save("search_history", history[:100])


def add_search_history(query: str):
    """検索履歴に追加する。"""
    history = load_search_history()
    entry = {"query": query, "timestamp": datetime.now().isoformat()}
    # 同じクエリがあれば削除して先頭に追加
    history = [h for h in history if h.get("query") != query]
    history.insert(0, entry)
    save_search_history(history)


def toggle_bookmark(article_no: str, text: str, vol: str, section_path: str):
    """ブックマークのトグル（追加/削除）。"""
    bookmarks = load_bookmarks()
    # 同じ条文番号+テキストハッシュで判定
    key = f"{article_no}_{hash(text)}"
    existing = [b for b in bookmarks if b.get("key") == key]
    if existing:
        bookmarks = [b for b in bookmarks if b.get("key") != key]
    else:
        bookmarks.append({
            "key": key,
            "article_no": article_no,
            "text": text[:500],
            "vol": vol,
            "section_path": section_path,
            "timestamp": datetime.now().isoformat(),
        })
    save_bookmarks(bookmarks)
    return not bool(existing)  # True=追加, False=削除


def is_bookmarked(article_no: str, text: str) -> bool:
    """ブックマーク済みか確認する。"""
    key = f"{article_no}_{hash(text)}"
    bookmarks = load_bookmarks()
    return any(b.get("key") == key for b in bookmarks)


# --- セッション初期化 ---
if "searcher" not in st.session_state:
    st.session_state.searcher = RRSearcher()

if "explanations" not in st.session_state:
    st.session_state.explanations = {}

if "search_results" not in st.session_state:
    st.session_state.search_results = []

if "all_hits" not in st.session_state:
    st.session_state.all_hits = []

if "total_hits" not in st.session_state:
    st.session_state.total_hits = 0

if "last_query" not in st.session_state:
    st.session_state.last_query = ""

# ツリー関連の状態
if "tree_data" not in st.session_state:
    st.session_state.tree_data = {}

if "active_tree" not in st.session_state:
    st.session_state.active_tree = None


# --- テキスト処理関数 ---
def _decode_font_shift(text: str) -> str:
    """PDFカスタムフォントのシフト文字化けをデコードする。"""
    result = []
    for c in text:
        code = ord(c)
        if 'A' <= c <= 'Z':
            result.append(chr((code - ord('A') - 3) % 26 + ord('A')))
        elif 'a' <= c <= 'z':
            result.append(chr((code - ord('a') - 3) % 26 + ord('a')))
        elif 33 <= code <= 64 or 91 <= code <= 96 or 123 <= code <= 126:
            decoded = code + 29
            if 32 <= decoded <= 126:
                result.append(chr(decoded))
            else:
                result.append(c)
        else:
            result.append(c)
    return ''.join(result)


def _vowel_ratio(text: str, min_len: int = 4) -> float:
    """テキストの母音比率を計算する。"""
    alpha = re.sub(r'[^a-zA-Z]', '', text)
    if len(alpha) < min_len:
        return 0.4
    vowels = sum(1 for c in alpha.lower() if c in 'aeiou')
    return vowels / len(alpha)


def fix_font_encoding(text: str) -> str:
    """フォントエンコーディング文字化けを検出・修復する（行単位対応）。"""
    alpha_only = re.sub(r'[^a-zA-Z]', '', text)
    if len(alpha_only) < 8:
        return text
    lines = text.split('\n')
    result_lines = []
    changed = False
    for line in lines:
        line_alpha = re.sub(r'[^a-zA-Z]', '', line)
        if len(line_alpha) >= 4:
            line_vr = _vowel_ratio(line)
            if line_vr < 0.28:
                decoded_line = _decode_font_shift(line)
                decoded_line_vr = _vowel_ratio(decoded_line)
                if decoded_line_vr - line_vr > 0.08 and decoded_line_vr >= 0.25:
                    result_lines.append(decoded_line)
                    changed = True
                    continue
        result_lines.append(line)
    return '\n'.join(result_lines) if changed else text


def clean_text(raw: str) -> str:
    """PDF抽出テキストのノイズを除去し、読みやすく整形する。"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", raw)
    text = re.sub(r"[\x80-\x9f]", "", text)
    text = re.sub(r"[\ue000-\uf8ff]", "", text)
    text = re.sub(
        r"[\u0590-\u083F\u0900-\u0DFF\u0F00-\u0FFF\u1200-\u137F"
        r"\u1B80-\u1BBF\uFB00-\uFDFF\uFE70-\uFEFF]", "", text)
    text = re.sub(r"[\ufffd\ufffe\uffff\u25a1\u25a0\u2610\u2612]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"–\s*\d+\s*–", "", text).strip()
    text = re.sub(r"^[a-z]{1,3}\s+", "", text)
    text = re.sub(r"(>>\s*)+", "", text).strip()
    text = re.sub(r"_{3,}", "", text).strip()
    text = re.sub(r"\.{3,}\s*\d*", "", text).strip()
    text = re.sub(r"^[A-Z]{2,4}\d+[-–]\d+\s*", "", text).strip()
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = fix_font_encoding(text)
    return text


def extract_title(text: str, article_no: str) -> str:
    """条文テキストからタイトル/セクション名を推定する。"""
    if article_no:
        after_no = re.sub(r"^No\.\s*\d+\.\d+[A-Z]?\s*", "", text).strip()
        match = re.match(r"^(.+?[.;])", after_no)
        if match and len(match.group(1)) <= 120:
            return match.group(1).strip()
        return after_no[:80].strip() + ("..." if len(after_no) > 80 else "")
    heading_match = re.match(
        r"^((?:ARTICLE|Article|RESOLUTION|Resolution|APPENDIX|Appendix|ANNEX|Annex|RECOMMENDATION|Recommendation)"
        r"\s*\d*[A-Z]?\s*[\-–:]?\s*.{0,80}?)(?:\s{2}|$)",
        text,
    )
    if heading_match:
        return heading_match.group(1).strip()
    match = re.match(r"^(.+?[.;])", text)
    if match and len(match.group(1)) <= 120:
        return match.group(1).strip()
    return text[:80].strip() + ("..." if len(text) > 80 else "")


def highlight_keywords(text: str, query: str) -> str:
    """検索クエリのフレーズ全体を赤字でハイライトする（HTML）。"""
    if not query or not text:
        return text
    phrase = query.strip()
    if not phrase:
        return text
    import html
    text = html.escape(text)
    phrase_escaped = html.escape(phrase)
    pattern = re.compile(re.escape(phrase_escaped), re.IGNORECASE)
    text = pattern.sub(
        lambda m: f'<span style="color: red; font-weight: bold;">{m.group()}</span>',
        text,
    )
    return text


def extract_paragraph(raw: str, max_len: int = 500) -> str:
    """テキストから最初の意味のあるパラグラフ（文単位）を抽出する。"""
    cleaned_t = clean_text(raw)
    if not cleaned_t:
        return ""
    protected = cleaned_t
    for abbr in ["Rec.", "No.", "Art.", "Rev.", "Vol.", "Res.", "Nos.", "etc.", "i.e.", "e.g."]:
        protected = protected.replace(abbr, abbr.replace(".", "\x00"))
    sentences = re.split(r"(?<=[.;])\s+", protected)
    sentences = [s.replace("\x00", ".") for s in sentences]
    sentences = [s for s in sentences if len(s) > 5]
    if not sentences:
        return cleaned_t[:max_len]
    result_text = ""
    for s in sentences:
        if len(result_text) + len(s) > max_len and len(result_text) >= 50:
            break
        result_text += s + " "
    return result_text.strip()


def get_claude_explanation(article_text: str, api_key: str) -> str:
    """Claude Haikuを使って条文の日本語解説を生成する。"""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"ITU無線通則の専門家として、以下の条文を日本語で150字以内で解説してください。\n\n条文:\n{article_text}",
            }],
        )
        return response.content[0].text
    except anthropic.APIError as e:
        return f"解説の生成に失敗しました: {e}"


# --- ツリー表示関数 ---
_DEPTH_COLORS = ["#1f77b4", "#2ca02c", "#d62728", "#9467bd", "#ff7f0e", "#8c564b"]


def _expand_node_action(node: dict, root_key: str):
    """ノード展開処理（Claude APIを呼び出して子ノードを追加）。"""
    number = node.get("number", "")
    text_preview = node.get("text_preview", "")
    searcher = st.session_state.searcher
    if searcher.chunks is None:
        searcher.load()

    article_text = text_preview
    if not article_text and number:
        found = find_article_text(searcher.chunks, number)
        if found:
            article_text = found

    if article_text:
        with st.spinner(f"{number} の関連条文を探索中..."):
            result = expand_node(number, article_text)

        new_children = []
        for rel in result.get("related", []):
            child_text = ""
            if searcher.chunks:
                found = find_article_text(searcher.chunks, rel["number"])
                if found:
                    child_text = found[:500]
            new_children.append({
                "node_id": rel.get("node_id", ""),
                "number": rel.get("number", ""),
                "reason": rel.get("reason", ""),
                "summary": "",
                "text_preview": child_text,
                "children": [],
            })

        node["summary"] = result.get("summary", "")
        node["children"] = new_children
        st.rerun()
    else:
        st.warning(f"条文 {number} のテキストが見つかりません。")


def _render_tree_node(node: dict, depth: int, root_key: str):
    """ツリーノードをStreamlitコンポーネントで再帰的にレンダリングする（ボタン付き）。"""
    import html as html_mod
    number = html_mod.escape(node.get("number", ""))
    summary = html_mod.escape(node.get("summary", ""))
    reason = html_mod.escape(node.get("reason", ""))
    text_preview = node.get("text_preview", "")
    children = node.get("children", [])
    node_id = node.get("node_id", "")
    color = _DEPTH_COLORS[depth % len(_DEPTH_COLORS)]
    is_leaf = len(children) == 0

    # インデント用のプレフィックス
    indent_px = depth * 32

    # ノードヘッダー（条文番号 + 展開ボタン）
    if depth == 0:
        # ルートノード
        header_html = f'<span style="font-size:1.2em; font-weight:bold; color:{color};">🔵 {number}</span>'
        if is_leaf:
            col_header, col_btn = st.columns([4, 1.5])
            with col_header:
                st.markdown(header_html, unsafe_allow_html=True)
            with col_btn:
                btn_key = f"expand_{root_key}_{node_id}_{depth}"
                if st.button("▶ 関連条文を探す", key=btn_key, type="secondary"):
                    _expand_node_action(node, root_key)
        else:
            st.markdown(header_html, unsafe_allow_html=True)
    else:
        # 子ノード
        connector = f'<span style="color:#999;">{"│　" * (depth - 1)}├─</span>'
        number_html = f'<span style="font-weight:bold; color:{color}; font-size:1.05em;">{number}</span>'

        if is_leaf:
            col_header, col_btn = st.columns([4, 1.5])
            with col_header:
                st.markdown(
                    f'<div style="margin-left:{indent_px}px; margin-top:4px;">'
                    f'{connector} {number_html}</div>',
                    unsafe_allow_html=True,
                )
            with col_btn:
                btn_key = f"expand_{root_key}_{node_id}_{depth}"
                if st.button("▶ 展開", key=btn_key, type="secondary"):
                    _expand_node_action(node, root_key)
        else:
            st.markdown(
                f'<div style="margin-left:{indent_px}px; margin-top:4px;">'
                f'{connector} {number_html}</div>',
                unsafe_allow_html=True,
            )

        # 関連理由
        if reason:
            st.markdown(
                f'<div style="margin-left:{indent_px + 24}px; color:#666; font-size:0.85em; margin-bottom:2px;">'
                f'💬 {reason}</div>',
                unsafe_allow_html=True,
            )

    # 要約
    if summary:
        s_indent = indent_px + (24 if depth > 0 else 0)
        st.markdown(
            f'<div style="margin-left:{s_indent}px; color:#333; font-size:0.9em; margin-bottom:2px;">'
            f'📝 {summary}</div>',
            unsafe_allow_html=True,
        )

    # 条文テキストプレビュー
    if text_preview:
        t_indent = indent_px + (24 if depth > 0 else 0)
        paragraph = extract_paragraph(text_preview, max_len=500)
        if paragraph:
            para_escaped = html_mod.escape(paragraph)
            st.markdown(
                f'<div style="margin-left:{t_indent}px; color:#555; font-size:0.85em; '
                f'line-height:1.5; margin-bottom:6px; padding:4px 8px; '
                f'background:#f5f5f5; border-radius:4px;">'
                f'{para_escaped}</div>',
                unsafe_allow_html=True,
            )

    # 子ノードを再帰的にレンダリング
    for child in children:
        _render_tree_node(child, depth + 1, root_key)


def render_tree(tree: dict, root_key: str):
    """ツリー全体をレンダリングする。"""
    with st.container(border=True):
        _render_tree_node(tree, depth=0, root_key=root_key)


# --- サイドバー ---
with st.sidebar:
    st.title("📡 ITU-RR 条文検索")
    st.markdown("ITU無線通則（Radio Regulations）の条文を自然言語で検索できます。")
    st.divider()

    # ページ選択
    page_mode = st.radio(
        "ページ",
        options=["search", "bookmarks", "history"],
        format_func=lambda x: {
            "search": "🔍 検索",
            "bookmarks": "⭐ ブックマーク",
            "history": "📜 検索履歴",
        }[x],
        index=0,
    )

    st.divider()

    search_mode = st.radio(
        "検索モード",
        options=["hybrid", "keyword", "semantic"],
        format_func=lambda x: {
            "hybrid": "🔍 ハイブリッド（推奨）",
            "keyword": "📝 キーワードのみ",
            "semantic": "🧠 意味検索のみ",
        }[x],
        index=0,
        help="ハイブリッド: キーワード一致 + 意味検索を統合\n"
             "キーワード: テキストに含まれるもののみ\n"
             "意味検索: AIが意味を理解して類似文を検索",
    )

    top_k = st.slider("カード表示件数", min_value=3, max_value=30, value=10)
    threshold = st.slider("スコアしきい値（%）", min_value=5, max_value=70, value=50, step=5,
                           help="この値以上のスコアを持つ条文のみテーブルに表示します")
    threshold_float = threshold / 100.0

    st.divider()

    # --- 検索範囲フィルタ ---
    st.caption("**📂 検索範囲**")
    vol_options = ["All", "Vol.1", "Vol.2", "Vol.3", "Vol.4", "RoP"]
    vol_labels = {
        "All": "📚 全Volume",
        "Vol.1": "📗 Vol.1（条文）",
        "Vol.2": "📘 Vol.2（周波数表・脚注）",
        "Vol.3": "📙 Vol.3（決議）",
        "Vol.4": "📕 Vol.4（Q信号等）",
        "RoP": "📔 Rules of Procedure",
    }
    vol_filter = st.radio(
        "Volume",
        options=vol_options,
        format_func=lambda x: vol_labels[x],
        index=0,
        label_visibility="collapsed",
    )

    # サブフィルタ（Volume選択に応じて表示）
    sub_filter = "All"
    if vol_filter == "Vol.1":
        # Vol.1: Article選択（よく使うものを上部に表示）
        _frequent = ["Article 5", "Article 9", "Article 11",
                      "Article 21", "Article 22"]
        _all_articles = [f"Article {n}" for n in range(1, 60)]
        _all_articles += ["Article 29A", "Article 29B", "Article 54A"]
        _others = [a for a in _all_articles if a not in _frequent]
        _article_options = ["All"] + _frequent + _others
        sub_filter = st.selectbox(
            "Article",
            options=_article_options,
            index=0,
            help="よく使うArticle（5, 9, 11, 21, 22）を上部に表示",
        )
    elif vol_filter == "Vol.2":
        # Vol.2: ANNEX選択
        _annex_options = ["All", "ANNEX 1", "ANNEX 2", "ANNEX 3",
                          "ANNEX 4", "ANNEX 5", "ANNEX 6", "ANNEX 7"]
        sub_filter = st.selectbox(
            "Annex",
            options=_annex_options,
            index=0,
        )

    st.divider()
    st.caption("**スコアの見方**")
    st.caption(
        "📝 **キーワード一致**: 検索フレーズがテキストに直接含まれている\n\n"
        "🧠 **意味検索**: AIが意味的に類似と判断（フレーズが含まれない場合あり）\n\n"
        "🔍 **ハイブリッド**: キーワード一致＋意味検索の両方でマッチ（最も高スコア）"
    )

    st.divider()
    if st.button("🔄 インデックス再作成", use_container_width=True):
        with st.spinner("インデックスを再作成中...（数分かかります）"):
            result = subprocess.run(
                [sys.executable, str(ROOT_DIR / "src" / "indexer.py"), "--force"],
                capture_output=True,
                text=True,
                cwd=str(ROOT_DIR),
            )
            if result.returncode == 0:
                st.session_state.searcher = RRSearcher()
                st.success("インデックスを再作成しました。")
            else:
                st.error(f"エラーが発生しました:\n{result.stderr}")

# --- メインエリア ---
searcher = st.session_state.searcher

# インデックスの存在確認
if not searcher.is_index_ready():
    st.warning(
        "⚠️ インデックスが未作成です。以下のコマンドでインデックスを作成してください:\n\n"
        "```\npython src/indexer.py\n```\n\n"
        "または、左サイドバーの「インデックス再作成」ボタンを押してください。"
    )
    st.stop()


# ========================================
# ブックマーク一覧ページ
# ========================================
if page_mode == "bookmarks":
    st.header("⭐ ブックマーク一覧")
    bookmarks = load_bookmarks()
    if not bookmarks:
        st.info("ブックマークされた条文はありません。\n検索結果の ⭐ ボタンでブックマークできます。")
    else:
        st.caption(f"{len(bookmarks)}件のブックマーク")
        for idx, bm in enumerate(bookmarks):
            with st.container(border=True):
                col1, col2 = st.columns([5, 1])
                with col1:
                    st.markdown(f"**📌 {bm.get('article_no', '')}**　`{bm.get('vol', '')}`")
                    if bm.get("section_path"):
                        st.caption(bm["section_path"])
                    preview = clean_text(bm.get("text", ""))[:200]
                    st.caption(preview)
                with col2:
                    ts = bm.get("timestamp", "")[:10]
                    st.caption(ts)
                    if st.button("🗑️", key=f"unbm_{idx}", help="ブックマーク解除"):
                        bookmarks = [b for b in bookmarks if b.get("key") != bm.get("key")]
                        save_bookmarks(bookmarks)
                        st.rerun()
    st.stop()

# ========================================
# 検索履歴ページ
# ========================================
if page_mode == "history":
    st.header("📜 検索履歴")
    history = load_search_history()
    if not history:
        st.info("検索履歴はありません。")
    else:
        st.caption(f"{len(history)}件の検索履歴")

        col_clear, _ = st.columns([1, 5])
        with col_clear:
            if st.button("🗑️ 履歴をクリア"):
                save_search_history([])
                st.rerun()

        for idx, entry in enumerate(history):
            col_q, col_ts, col_btn = st.columns([3, 1.5, 1])
            with col_q:
                st.write(f"**{entry.get('query', '')}**")
            with col_ts:
                ts = entry.get("timestamp", "")[:16].replace("T", " ")
                st.caption(ts)
            with col_btn:
                if st.button("🔍 再検索", key=f"hist_{idx}"):
                    st.session_state.last_query = entry["query"]
                    with st.spinner("検索中..."):
                        search_data = searcher.search(
                            entry["query"], top_k=top_k,
                            threshold=threshold_float, mode=search_mode,
                            vol_filter=vol_filter, sub_filter=sub_filter
                        )
                        st.session_state.search_results = search_data["results"]
                        st.session_state.all_hits = search_data["all_hits"]
                        st.session_state.total_hits = search_data["total_hits"]
                    st.rerun()
    st.stop()

# ========================================
# 検索ページ（メイン）
# ========================================

# ツリーが開いている場合は先に表示
if st.session_state.active_tree and st.session_state.active_tree in st.session_state.tree_data:
    tree_key = st.session_state.active_tree
    tree = st.session_state.tree_data[tree_key]

    st.subheader(f"🌳 関連条文ツリー — {tree.get('number', '')}")

    tree_btn_col1, tree_btn_col2, tree_btn_col3 = st.columns([1.5, 1.5, 3])
    with tree_btn_col1:
        md = build_tree_markdown(tree)
        number_safe = re.sub(r"[^a-zA-Z0-9._-]", "_", tree.get("number", "tree"))
        filename = f"RR_{number_safe}_tree.md"
        st.download_button(
            "📥 Markdownエクスポート",
            data=md,
            file_name=filename,
            mime="text/markdown",
            key="tree_download",
            use_container_width=True,
        )
    with tree_btn_col2:
        if st.button("✖ ツリーを閉じる", key="tree_close", use_container_width=True):
            st.session_state.active_tree = None
            st.rerun()

    render_tree(tree, root_key=tree_key)
    st.divider()

# 検索フォーム
query = st.text_input(
    "🔍 検索キーワードを入力（英語のみ）",
    placeholder="e.g. NGSO protection, frequency allocation, harmful interference...",
)

# 検索ボタン
if st.button("検索", type="primary", use_container_width=True):
    if not query:
        st.info("検索キーワードを入力してください。")
    else:
        st.session_state.last_query = query
        add_search_history(query)
        with st.spinner("検索中..."):
            try:
                search_data = searcher.search(query, top_k=top_k, threshold=threshold_float, mode=search_mode,
                                                          vol_filter=vol_filter, sub_filter=sub_filter)
                st.session_state.search_results = search_data["results"]
                st.session_state.all_hits = search_data["all_hits"]
                st.session_state.total_hits = search_data["total_hits"]
            except FileNotFoundError as e:
                st.error(str(e))
                st.stop()

# 検索結果の表示
results = st.session_state.search_results

if results:
    total = st.session_state.total_hits
    # フィルタ表示
    filter_label = ""
    if vol_filter != "All":
        filter_label = f" [{vol_filter}"
        if sub_filter != "All":
            filter_label += f" > {sub_filter}"
        filter_label += "]"
    st.subheader(f"検索結果（{len(results)}件 / {total}件ヒット）— 「{st.session_state.last_query}」{filter_label}")

    for i, result in enumerate(results):
        score_pct = result["score"] * 100
        article = result["article_no"] or ""
        vol = result["vol"]
        pdf_page = result.get("pdf_page", result.get("page", 0))
        printed_page = result.get("printed_page", "")
        section_path = result.get("section_path", "")
        raw_text = result["text"]
        cleaned = clean_text(raw_text)
        title = extract_title(cleaned, article)
        cache_key = hash(raw_text)

        if printed_page:
            page_display = f"p.{printed_page}"
        else:
            page_display = f"p.{pdf_page}"

        with st.container(border=True):
            # ヘッダー行
            col1, col2 = st.columns([4, 1])
            with col1:
                label = f"📌 **{article}**" if article else "📄"
                st.markdown(f"{label}　`{vol}`　{page_display}")
            with col2:
                match_type = result.get("match_type", "")
                type_icon = {"keyword": "📝", "semantic": "🧠", "hybrid": "🔍"}.get(match_type, "")
                st.metric("スコア", f"{score_pct:.1f}%", delta=type_icon, delta_color="off")

            # 文書構造パス
            if section_path:
                path_parts = [p.strip() for p in section_path.split(">")]
                tree_lines = []
                for depth, part in enumerate(path_parts):
                    indent_str = "　" * depth
                    connector = "📁" if depth == 0 else "└"
                    tree_lines.append(f"{indent_str}{connector} {part}")
                st.markdown("  \n".join(tree_lines))

            # 本文表示
            paragraph = extract_paragraph(raw_text, max_len=500)
            if paragraph:
                highlighted = highlight_keywords(paragraph, st.session_state.last_query)
                st.markdown(highlighted, unsafe_allow_html=True)

            # ボタン行: Claude解説 + 関連条文ツリー + ブックマーク
            btn_col1, btn_col2, btn_col3, _ = st.columns([1, 1.5, 0.5, 3])

            with btn_col1:
                if st.button("💡 Claude解説", key=f"explain_{i}"):
                    if cache_key not in st.session_state.explanations:
                        api_key = os.getenv("ANTHROPIC_API_KEY")
                        if not api_key:
                            st.error("ANTHROPIC_API_KEYが設定されていません。")
                        else:
                            with st.spinner("Claude Haikuが解説を生成中..."):
                                explanation = get_claude_explanation(cleaned, api_key)
                                st.session_state.explanations[cache_key] = explanation

            with btn_col2:
                tree_key = f"tree_{article}_{i}" if article else f"tree_noart_{i}"
                if st.button("🌳 関連条文ツリー", key=f"treebtn_{i}"):
                    root_node = {
                        "node_id": f"root_{i}",
                        "number": article or "(条文番号なし)",
                        "summary": "",
                        "reason": "",
                        "text_preview": cleaned[:500],
                        "children": [],
                    }
                    st.session_state.tree_data[tree_key] = root_node
                    st.session_state.active_tree = tree_key
                    st.rerun()

            with btn_col3:
                bm_icon = "⭐" if is_bookmarked(article, raw_text) else "☆"
                if st.button(bm_icon, key=f"bm_{i}", help="ブックマーク"):
                    added = toggle_bookmark(article, raw_text, vol, section_path)
                    st.rerun()

            # キャッシュされた解説
            if cache_key in st.session_state.explanations:
                st.info(st.session_state.explanations[cache_key])

    # --- 全件テーブル表示 ---
    all_hits = st.session_state.all_hits
    if all_hits:
        st.divider()
        st.subheader(f"📋 全ヒット一覧（{len(all_hits)}件）— Vol / ページ順")

        def vol_sort_key(vol_str):
            m = re.match(r"Vol\.(\d+)", vol_str)
            return int(m.group(1)) if m else 99

        def page_sort_key(r):
            pp = r.get("printed_page", "")
            if pp and pp.isdigit():
                return int(pp)
            return r.get("pdf_page", r.get("page", 9999))

        sorted_hits = sorted(all_hits, key=lambda r: (vol_sort_key(r["vol"]), page_sort_key(r)))

        header_cols = st.columns([0.6, 0.5, 0.7, 2.0, 0.6, 3.0, 0.8])
        headers = ["Vol", "Page", "条文番号", "セクション", "スコア", "内容", "解説"]
        for col, h in zip(header_cols, headers):
            col.markdown(f"**{h}**")
        st.markdown("---")

        for idx, result in enumerate(sorted_hits):
            pp = result.get("printed_page", "")
            dp = result.get("pdf_page", result.get("page", ""))
            page_str = pp if pp else str(dp)
            paragraph = extract_paragraph(result["text"])
            cleaned_full = clean_text(result["text"])
            tbl_cache_key = f"tbl_{hash(result['text'])}"

            row_cols = st.columns([0.6, 0.5, 0.7, 2.0, 0.6, 3.0, 0.8])
            row_cols[0].write(result["vol"])
            row_cols[1].write(page_str)
            row_cols[2].write(result["article_no"] or "—")
            row_cols[3].caption(result.get("section_path", ""))
            row_cols[4].write(f"{result['score'] * 100:.1f}%")
            highlighted_para = highlight_keywords(paragraph, st.session_state.last_query)
            row_cols[5].markdown(
                f'<span style="font-size: 0.85em; color: #666;">{highlighted_para}</span>',
                unsafe_allow_html=True,
            )

            if row_cols[6].button("💡", key=f"tbl_explain_{idx}", help="Claude解説を表示"):
                if tbl_cache_key not in st.session_state.explanations:
                    api_key = os.getenv("ANTHROPIC_API_KEY")
                    if not api_key:
                        st.error("ANTHROPIC_API_KEYが設定されていません。")
                    else:
                        with st.spinner("解説生成中..."):
                            explanation = get_claude_explanation(cleaned_full, api_key)
                            st.session_state.explanations[tbl_cache_key] = explanation

            if tbl_cache_key in st.session_state.explanations:
                st.info(st.session_state.explanations[tbl_cache_key])
