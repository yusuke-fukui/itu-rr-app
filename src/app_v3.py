"""
ITU-RR 条文参照グラフ v3
双方向トラバーサルによる条文間リンク表示アプリ。
ルート切替+パンくず履歴方式。
Small Satellite Handbook 手続き構造対応。
"""

import hashlib
import json
import os
import re
from pathlib import Path

import anthropic
import streamlit as st
from dotenv import load_dotenv

# パス設定
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

GRAPH_PATH = ROOT_DIR / "data" / "graph" / "reference_graph.json"
ARTICLES_PATH = ROOT_DIR / "data" / "graph" / "articles.json"
HANDBOOK_PATH = ROOT_DIR / "data" / "graph" / "handbook_overlay.json"
CACHE_PATH = ROOT_DIR / "data" / "graph" / "summary_cache.json"

SUMMARY_MODEL = "claude-sonnet-4-20250514"


# ─────────────────────────────────────────
# データ読み込み
# ─────────────────────────────────────────

@st.cache_data
def load_graph():
    with open(GRAPH_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data
def load_articles():
    with open(ARTICLES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CHUNKS_INDEX_PATH = ROOT_DIR / "data" / "index" / "chunks.json"


def _extract_footnotes_from_text(text: str, index: dict, entry_pat):
    """テキスト中の脚注区切り線以降から脚注エントリを抽出してindexに追加する。"""
    divider_pat = re.compile(r'_{10,}')
    m = divider_pat.search(text)
    if not m:
        return
    fn_raw = text[m.end():]
    # 脚注番号付きエントリ: "21 11.41.2 When submitting..."
    matches = list(entry_pat.finditer(fn_raw))
    # 脚注番号なしエントリも拾う: "11.41.1 (SUP - WRC-12)"
    bare_pat = re.compile(r'(?:^|\n)(\d+\.\d+[A-Z]?\.\d+)\s', re.MULTILINE)
    bare_matches = list(bare_pat.finditer(fn_raw))

    # 全エントリの位置をマージしてソート
    all_entries = []
    for match in matches:
        sub_num = match.group(2)
        pos = match.start()
        if fn_raw[pos] == '\n':
            pos += 1
        all_entries.append((pos, sub_num))
    for match in bare_matches:
        sub_num = match.group(1)
        pos = match.start()
        if fn_raw[pos] == '\n':
            pos += 1
        # 重複回避（既に番号付きで取得済みの場合）
        if not any(e[1] == sub_num for e in all_entries):
            all_entries.append((pos, sub_num))
    all_entries.sort(key=lambda x: x[0])

    for i, (pos, sub_num) in enumerate(all_entries):
        end = all_entries[i + 1][0] if i + 1 < len(all_entries) else len(fn_raw)
        full_text = fn_raw[pos:end].strip()
        if not full_text:
            continue
        parent_match = re.match(r'(\d+\.\d+[A-Z]?)\.\d+', sub_num)
        if parent_match:
            parent_num = parent_match.group(1)
            # 重複追加回避
            if parent_num not in index:
                index[parent_num] = []
            if full_text not in index[parent_num]:
                index[parent_num].append(full_text)


def build_footnote_index(articles: dict) -> dict:
    """全条文テキスト + チャンクから脚注をパースし、条文番号→脚注テキストの索引を構築する。
    Returns: {"11.28": ["11 11.28.1\\nIn case of...", ...], "11.31": [...], ...}
    """
    entry_pat = re.compile(r'(?:^|\n)(\d+)\s+(\d+\.\d+[A-Z]?\.\d+)\s', re.MULTILINE)
    index = {}  # article_num -> [footnote_text, ...]

    # 1. articles.json から抽出
    for art_num, art in articles.items():
        text = art.get("text", "")
        _extract_footnotes_from_text(text, index, entry_pat)

    # 2. chunks.json から補完（articles.jsonに含まれない脚注を拾う）
    if CHUNKS_INDEX_PATH.exists():
        with open(CHUNKS_INDEX_PATH, "r", encoding="utf-8") as f:
            chunks = json.load(f)
        for chunk in chunks:
            text = chunk.get("text", "")
            if "___" in text:
                _extract_footnotes_from_text(text, index, entry_pat)

    # 3. 手動修正データ（PDF抽出の文字化け・欠落を補正）
    footnote_corrections = {
        "11.41": [
            "11.41.1     (SUP - WRC-12)",
            "21 11.41.2  When submitting notices in application of No. 11.41, the notifying administration shall indicate to the Bureau that efforts have been made to effect coordination with those administrations whose assignments were the basis of the unfavourable findings under No. 11.38, without success.     (WRC-12)",
        ],
    }
    for art_num, entries in footnote_corrections.items():
        index[art_num] = entries  # 手動データで上書き

    # 4. 重複排除（同じ脚注が複数ソースから拾われる場合）
    for art_num in index:
        seen = set()
        unique = []
        for entry in index[art_num]:
            # 先頭50文字をキーにして重複判定
            key = entry[:50].strip()
            if key not in seen:
                seen.add(key)
                unique.append(entry)
        index[art_num] = unique

    return index


ROP_SECTIONS_PATH = ROOT_DIR / "data" / "graph" / "rop_sections.json"


def build_rop_index() -> dict:
    """RoP PDFから抽出済みのセクションデータを読み込む。
    rop_sections.json: 四角囲み/太字のセクション見出しで分割した全文テキスト。
    Returns: {"9.21": ["full RoP text..."], "11.31": ["..."], ...}
    """
    if not ROP_SECTIONS_PATH.exists():
        return {}
    with open(ROP_SECTIONS_PATH, "r", encoding="utf-8") as f:
        sections = json.load(f)

    # 各セクションを1エントリのリストとして返す
    index = {}
    for art_no, text in sections.items():
        text = text.strip()
        if text:
            index[art_no] = [text]

    # 複合セクション見出し（例: "11.44B, 11.44C, 11.44D and 11.44E"）を
    # 個別の条文にも紐付ける
    _split_combined_sections(index)

    return index


def _split_combined_sections(index: dict):
    """セクション内に複合見出し行がある場合、その内容を個別条文にも紐付ける。
    例: 11.44 内の "11.44B, 11.44C, 11.44D and 11.44E" 以降を各条文に登録。
    """
    # パターン: "11.44B, 11.44C, 11.44D and 11.44E" のような行
    combined_pat = re.compile(
        r'^(\d+\.\d+[A-Z](?:\.\d+)?)'  # 最初の条文番号
        r'(?:,\s*\d+\.\d+[A-Z](?:\.\d+)?)*'  # カンマ区切りの追加番号
        r'(?:\s+and\s+(\d+\.\d+[A-Z](?:\.\d+)?))?$'  # "and" の最後の番号
    )

    updates = {}
    for art_no, texts in list(index.items()):
        for text in texts:
            paragraphs = text.split('\n\n')
            for i, para in enumerate(paragraphs):
                clean = para.strip()
                m = combined_pat.match(clean)
                if m and i + 1 < len(paragraphs):
                    # この行以降のテキストを抽出
                    sub_text = '\n\n'.join(paragraphs[i + 1:]).strip()
                    if not sub_text:
                        continue
                    # 全ての条文番号を抽出
                    sub_arts = re.findall(r'(\d+\.\d+[A-Z](?:\.\d+)?)', clean)
                    for sub_art in sub_arts:
                        if sub_art != art_no:
                            if sub_art not in updates:
                                updates[sub_art] = []
                            updates[sub_art].append(sub_text)
                    # 親セクションのテキストをこの見出し行の前までに切る
                    index[art_no] = ['\n\n'.join(paragraphs[:i]).strip()]
                    break  # 1セクションにつき1つの複合見出しのみ

    for art_no, texts in updates.items():
        if art_no in index:
            index[art_no].extend(texts)
        else:
            index[art_no] = texts


def load_handbook():
    if HANDBOOK_PATH.exists():
        with open(HANDBOOK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_summary_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_summary_cache(cache: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────

def sort_key(num: str) -> tuple:
    """条文番号を数値ソート。9.4 < 9.11 < 11.3"""
    m = re.match(r'(\d+)\.(\d+)([A-Z]*)', num)
    if not m:
        return (999, 999, num)
    return (int(m.group(1)), int(m.group(2)), m.group(3))


def validate_article_number(query: str) -> str | None:
    query = query.strip()
    m = re.match(r'^(?:No\.?\s*)?(\d+\.\d+[A-Z]?)$', query, re.IGNORECASE)
    return m.group(1) if m else None


# ─────────────────────────────────────────
# ナビゲーション（ルート切替+履歴）
# ─────────────────────────────────────────

def navigate_to(num: str):
    """条文にナビゲートする。履歴にプッシュ。"""
    if "history" not in st.session_state:
        st.session_state["history"] = []

    current = st.session_state.get("current_article")
    if current and current != num:
        st.session_state["history"].append(current)

    st.session_state["current_article"] = num


def navigate_back():
    """履歴を1つ戻る。"""
    history = st.session_state.get("history", [])
    if history:
        prev = history.pop()
        st.session_state["current_article"] = prev
        st.session_state["history"] = history


# ─────────────────────────────────────────
# 条文テキスト表示
# ─────────────────────────────────────────

def parse_footnote_entries(footnotes_raw: str) -> list:
    """脚注テキストを個別エントリに分割する。
    各脚注は "番号 条文サブ番号 テキスト" の形式。
    例: "15 11.32A.1 The examination of such notices..."
    Returns: [(footnote_num, sub_number, full_text), ...]
    """
    # 脚注エントリの開始パターン: 行頭の数字 + スペース + 条文サブ番号
    entry_pattern = re.compile(
        r'(?:^|\n)(\d+)\s+(\d+\.\d+[A-Z]?\.\d+)\s',
    )
    matches = list(entry_pattern.finditer(footnotes_raw))
    entries = []
    for i, m in enumerate(matches):
        fn_num = m.group(1)
        sub_num = m.group(2)
        start = m.start()
        if footnotes_raw[start] == '\n':
            start += 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(footnotes_raw)
        full_text = footnotes_raw[start:end].strip()
        entries.append((fn_num, sub_num, full_text))
    return entries


def filter_footnotes_for_article(footnotes_raw: str, article_num: str) -> str:
    """条文番号に該当する脚注のみを返す。
    例: article_num="11.32A" → "11.32A.1", "11.32A.2" の脚注のみ返す。
    """
    entries = parse_footnote_entries(footnotes_raw)
    if not entries:
        return footnotes_raw  # パース失敗時はそのまま返す

    # 条文番号プレフィックスでフィルタ (例: "11.32A.")
    prefix = article_num + "."
    matched = [text for fn_num, sub_num, text in entries if sub_num.startswith(prefix)]

    if matched:
        return "\n\n".join(matched)
    # マッチなしでもエントリがある場合は空（他の条文の脚注のみ）
    if entries:
        return None
    return footnotes_raw


def split_footnotes(text: str, article_num: str = ""):
    """条文テキストから脚注を分離する。
    区切り線 _______________ 以降を脚注として扱い、
    article_num に該当する脚注のみ返す。"""
    # 区切り線パターン: 連続アンダースコア（10個以上）、末尾でもマッチ
    divider_pattern = re.compile(r'\n?_{10,}\n?')
    match = divider_pattern.search(text)
    if match:
        body = text[:match.start()].strip()
        footnotes_raw = text[match.end():].strip()
        # 本文末尾の脚注参照番号を整理（例: ";15, 16 or     (WRC-15)"）
        body = re.sub(r';\s*\d+(?:\s*,\s*\d+)*\s+or\s*$', ';', body, flags=re.MULTILINE)
        # 条文番号でフィルタ
        if article_num and footnotes_raw:
            filtered = filter_footnotes_for_article(footnotes_raw, article_num)
            return body, filtered
        return body, footnotes_raw
    return text, None


def clean_inline_footnote_refs(body: str) -> str:
    """本文中に埋め込まれた脚注参照番号を除去する。
    例: "notice11" → "notice", "Allocations12" → "Allocations"
         "sub-paragraphs;14" → "sub-paragraphs;"
    ただし条文番号（9.12, 11.31等）は除去しない。"""
    # 英字/セミコロン/閉じ括弧の直後に続く1-2桁の数字（上付き文字のプレーンテキスト化）
    # 条文番号パターン（数字.数字）の一部は除外
    cleaned = re.sub(r'([a-zA-Z);])(\d{1,2})(?=[\s\.\,\n]|$)', r'\1', body)
    return cleaned


def render_article_text(num: str, articles: dict, footnote_index: dict = None):
    art = articles.get(num, {})
    text = art.get("text", "")
    vol = art.get("vol", "")

    if text:
        body, footnotes = split_footnotes(text, article_num=num)

        # 本文中の脚注参照番号を除去
        body = clean_inline_footnote_refs(body)

        # 脚注が本文内になかった場合、グローバル脚注インデックスから取得
        if not footnotes and footnote_index and num in footnote_index:
            footnotes = "\n\n".join(footnote_index[num])

        display_text = body[:2000]
        if len(body) > 2000:
            display_text += "..."
        vol_badge = f"<span style='background:#e3f2fd;padding:2px 6px;border-radius:3px;font-size:0.75em;margin-left:8px;'>{vol}</span>" if vol else ""
        st.markdown(
            f'<div style="background:#f8f9fa; border-left:3px solid #1976d2; '
            f'padding:12px 16px; margin:8px 0; font-size:0.9em; '
            f'color:#333; max-height:400px; overflow-y:auto; line-height:1.6;">'
            f'{display_text}</div>',
            unsafe_allow_html=True
        )
        if footnotes:
            with st.expander(f"📝 脚注 (Footnotes)", expanded=False):
                st.markdown(
                    f'<div style="background:#fffde7; border-left:3px solid #fbc02d; '
                    f'padding:10px 14px; font-size:0.8em; color:#555; line-height:1.5;">'
                    f'{footnotes[:3000]}</div>',
                    unsafe_allow_html=True
                )
    else:
        st.caption("（条文テキスト未収録）")


# ─────────────────────────────────────────
# メイン表示：ルートノード + 参照一覧
# ─────────────────────────────────────────

def render_root(num: str, graph: dict, articles: dict,
                condition_labels: dict, handbook_notes: dict,
                procedure_routes: dict, footnote_index: dict = None,
                rop_index: dict = None):
    """ルートノードと参照元/参照先の一覧を表示。"""
    node = graph.get(num, {})
    refs_from = node.get("refs_from", [])
    refs_to = node.get("refs_to", [])

    # パンくず履歴
    history = st.session_state.get("history", [])
    if history:
        breadcrumb_parts = []
        for i, h in enumerate(history):
            breadcrumb_parts.append(f"`{h}`")
        breadcrumb = " → ".join(breadcrumb_parts) + f" → **{num}**"

        col_bc, col_back = st.columns([5, 1])
        with col_bc:
            st.markdown(f"🔙 {breadcrumb}")
        with col_back:
            if st.button("← 戻る", key="back_btn"):
                navigate_back()
                st.rerun()

    # アクティブルートインジケーター
    active_route_id = st.session_state.get("active_route")
    if active_route_id and active_route_id in procedure_routes:
        route = procedure_routes[active_route_id]
        steps = route["steps"]
        step_articles = [s["article"] for s in steps]

        if num in step_articles:
            idx = step_articles.index(num)
            progress = (idx + 1) / len(steps)
            st.progress(progress)
            st.markdown(
                f'<div style="background:#e8f5e9; border-left:3px solid #4caf50; '
                f'padding:8px 12px; margin:4px 0; font-size:0.85em; border-radius:4px;">'
                f'🗺 <b>{route["name"]}</b> — '
                f'Step {idx+1}/{len(steps)}: {steps[idx]["label"]}'
                f'</div>',
                unsafe_allow_html=True,
            )

            nav_col1, nav_col2, nav_col3 = st.columns([1, 3, 1])
            with nav_col1:
                if idx > 0:
                    if st.button(f"← No. {step_articles[idx-1]}", key="route_prev"):
                        navigate_to(step_articles[idx - 1])
                        st.rerun()
            with nav_col3:
                if idx < len(steps) - 1:
                    if st.button(f"No. {step_articles[idx+1]} →", key="route_next"):
                        navigate_to(step_articles[idx + 1])
                        st.rerun()
        else:
            # ルート外の条文に移動した場合
            st.caption(f"🗺 {route['name']} — ルート外の条文を表示中")
            if st.button("✕ ルートを終了", key="exit_route"):
                del st.session_state["active_route"]
                st.rerun()

    # ルートノード見出し
    st.markdown(f"## 🔵 No. {num}")
    render_article_text(num, articles, footnote_index=footnote_index)

    # ハンドブック注釈
    note = handbook_notes.get(num)
    if note:
        with st.expander("📘 Handbook Note", expanded=False):
            st.markdown(note["note"])
            st.caption(f"Source: Small Satellite Handbook 2023, {note['section']}")

    # RoP注釈
    if rop_index and num in rop_index:
        rop_texts = rop_index[num]
        with st.expander("📜 Rules of Procedure", expanded=False):
            for i, rop_text in enumerate(rop_texts):
                # 段落を維持して表示（\n\n → <br><br>）
                paragraphs = rop_text.strip().split("\n\n")
                html_parts = []
                for para in paragraphs:
                    clean = para.replace("\n", " ").strip()
                    if clean:
                        html_parts.append(f"<p style='margin:0 0 8px 0;'>{clean}</p>")
                html_content = "".join(html_parts)
                st.markdown(
                    f'<div style="background:#fff8e1; border-left:3px solid #ffa726; '
                    f'padding:8px 12px; margin:4px 0; font-size:0.85em; border-radius:4px;">'
                    f'{html_content}</div>',
                    unsafe_allow_html=True,
                )
                if i < len(rop_texts) - 1:
                    st.markdown("---")
            st.caption("Source: Rules of Procedure (2021 edition, rev.2)")

    st.divider()

    # 参照元と参照先を左右2カラム
    col_from, col_to = st.columns(2)

    with col_from:
        st.markdown(f"### ◀ 参照元（{len(refs_to)}件）")
        st.caption("この条文が参照している条文")
        if refs_to:
            for ref in refs_to:
                ref_art = articles.get(ref, {})
                ref_text = ref_art.get("text", "")[:120]
                ref_vol = ref_art.get("vol", "")

                # 条件ラベル lookup: この条文(num) → 参照先(ref)
                edge_key = f"{num} -> {ref}"
                label = condition_labels.get(edge_key, "")

                with st.container():
                    c1, c2 = st.columns([4, 1])
                    with c1:
                        vol_tag = f" `{ref_vol}`" if ref_vol else ""
                        st.markdown(f"**No. {ref}**{vol_tag}")
                        if label:
                            st.markdown(
                                f'<span style="background:#fff3e0;padding:2px 8px;'
                                f'border-radius:10px;font-size:0.75em;color:#e65100;">'
                                f'{label}</span>',
                                unsafe_allow_html=True,
                            )
                        if ref_text:
                            st.caption(ref_text + ("..." if len(ref_art.get("text", "")) > 120 else ""))
                    with c2:
                        if st.button("→", key=f"goto_to_{ref}", help=f"No. {ref} に移動"):
                            navigate_to(ref)
                            st.rerun()
                    st.markdown("---")
        else:
            st.info("なし")

    with col_to:
        st.markdown(f"### ▶ 参照先（{len(refs_from)}件）")
        st.caption("この条文を参照している条文")
        if refs_from:
            for ref in refs_from:
                ref_art = articles.get(ref, {})
                ref_text = ref_art.get("text", "")[:120]
                ref_vol = ref_art.get("vol", "")

                # 条件ラベル lookup: 参照元(ref) → この条文(num)
                edge_key = f"{ref} -> {num}"
                label = condition_labels.get(edge_key, "")

                with st.container():
                    c1, c2 = st.columns([4, 1])
                    with c1:
                        vol_tag = f" `{ref_vol}`" if ref_vol else ""
                        st.markdown(f"**No. {ref}**{vol_tag}")
                        if label:
                            st.markdown(
                                f'<span style="background:#fff3e0;padding:2px 8px;'
                                f'border-radius:10px;font-size:0.75em;color:#e65100;">'
                                f'{label}</span>',
                                unsafe_allow_html=True,
                            )
                        if ref_text:
                            st.caption(ref_text + ("..." if len(ref_art.get("text", "")) > 120 else ""))
                    with c2:
                        if st.button("→", key=f"goto_from_{ref}", help=f"No. {ref} に移動"):
                            navigate_to(ref)
                            st.rerun()
                    st.markdown("---")
        else:
            st.info("なし")


# ─────────────────────────────────────────
# AIサマリー
# ─────────────────────────────────────────

def generate_flow_summary(article_nums: list, articles: dict) -> str:
    cache = load_summary_cache()
    cache_key = hashlib.md5(",".join(sorted(article_nums)).encode()).hexdigest()

    if cache_key in cache:
        return cache[cache_key]

    context_parts = []
    for num in article_nums:
        art = articles.get(num, {})
        text = art.get("text", "")[:500]
        if text:
            context_parts.append(f"No. {num}:\n{text}")

    context = "\n\n".join(context_parts)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return "APIキーが設定されていません。"

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=2000,
            system="あなたはITU Radio Regulations（無線通信規則）の専門家です。",
            messages=[{
                "role": "user",
                "content": f"""以下の条文群は、ユーザーが条文参照グラフを辿ったルートです。
このフロー全体を英語と日本語の両方で整理してください。

出力形式:
## English Summary
（条文間の関係とフローの概要を2-3段落で）

## 日本語サマリー
（同内容を日本語で）

条文群:
{context}"""
            }],
        )
        summary = response.content[0].text.strip()
        cache[cache_key] = summary
        save_summary_cache(cache)
        return summary

    except Exception as e:
        return f"サマリー生成エラー: {e}"


# ─────────────────────────────────────────
# メインアプリ
# ─────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="ITU-RR 条文参照グラフ",
        page_icon="🔗",
        layout="wide",
    )

    st.title("🔗 ITU-RR 条文参照グラフ")
    st.caption("条文間の明示的な参照関係を双方向でトラバーサルする")

    graph = load_graph()
    articles = load_articles()
    footnote_index = build_footnote_index(articles)
    handbook = load_handbook()
    rop_index = build_rop_index()

    condition_labels = handbook.get("condition_labels", {})
    handbook_notes = handbook.get("handbook_notes", {})
    procedure_routes = handbook.get("procedure_routes", {})

    # サイドバー
    with st.sidebar:
        # 手続きルート
        if procedure_routes:
            st.header("🗺 手続きルート")
            for route_id, route in procedure_routes.items():
                with st.expander(route["name"], expanded=False):
                    st.caption(route["description"])
                    if route.get("handbook_section"):
                        st.caption(f"📘 {route['handbook_section']}")

                    for i, step in enumerate(route["steps"]):
                        art = step["article"]
                        label = step["label"]
                        col_step, col_btn = st.columns([4, 1])
                        with col_step:
                            in_graph = art in graph
                            marker = "●" if in_graph else "○"
                            st.markdown(f"{marker} **{i+1}.** No. {art} — {label}")
                        with col_btn:
                            if art in graph:
                                if st.button("→", key=f"route_{route_id}_{art}"):
                                    st.session_state["active_route"] = route_id
                                    st.session_state["history"] = []
                                    st.session_state["current_article"] = art
                                    st.rerun()

                    if st.button(f"▶ このルートを開始", key=f"start_route_{route_id}"):
                        steps = route["steps"]
                        st.session_state["history"] = []
                        st.session_state["current_article"] = steps[0]["article"]
                        st.session_state["active_route"] = route_id
                        st.rerun()

            st.divider()

    # 検索入力
    col_input, col_btn = st.columns([4, 1])
    with col_input:
        query = st.text_input(
            "🔍 条文番号を入力（例: 9.12, 11.31）",
            placeholder="9.12",
            key="search_input",
        )
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        search_clicked = st.button("検索", type="primary", use_container_width=True)

    if search_clicked and query:
        num = validate_article_number(query)
        if num is None:
            st.error("❌ 条文番号の形式が正しくありません。例: 9.12, 11.31, 5.364")
        elif num not in graph:
            st.warning(f"⚠️ No. {num} は参照グラフに存在しません。")
        else:
            if "active_route" in st.session_state:
                del st.session_state["active_route"]
            st.session_state["history"] = []
            st.session_state["current_article"] = num
            st.rerun()

    # メイン表示
    current = st.session_state.get("current_article")
    if current and current in graph:
        st.divider()
        render_root(current, graph, articles,
                    condition_labels, handbook_notes, procedure_routes,
                    footnote_index=footnote_index, rop_index=rop_index)

        # AIサマリーボタン
        st.divider()
        history = st.session_state.get("history", [])
        trail = history + [current]
        if len(trail) >= 2:
            trail_str = " → ".join(trail)
            st.caption(f"📍 辿ったルート: {trail_str}")

            if st.button("🤖 このルートをAIで整理", type="secondary"):
                with st.spinner(f"AIが {len(trail)} 条文のフローを整理中..."):
                    summary = generate_flow_summary(trail, articles)
                st.markdown(summary)
        else:
            st.caption("💡 条文を辿っていくと、AIでフロー全体を整理できます。")

    else:
        st.info("👆 条文番号を入力するか、サイドバーの手続きルートから条文を選んでください。")

        art9 = [k for k in graph if k.startswith("9.")]
        art11 = [k for k in graph if k.startswith("11.")]

        with st.expander("📋 Article 9 の条文一覧", expanded=False):
            for num in sorted(art9, key=sort_key):
                refs_to = len(graph[num].get("refs_to", []))
                refs_from = len(graph[num].get("refs_from", []))
                st.markdown(f"**No. {num}** — 参照先: {refs_to}, 参照元: {refs_from}")

        with st.expander("📋 Article 11 の条文一覧", expanded=False):
            for num in sorted(art11, key=sort_key):
                refs_to = len(graph[num].get("refs_to", []))
                refs_from = len(graph[num].get("refs_from", []))
                st.markdown(f"**No. {num}** — 参照先: {refs_to}, 参照元: {refs_from}")


if __name__ == "__main__":
    main()
