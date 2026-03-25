"""
Article 9・11の条文をVol.1 PDFから直接パースする。
各条文番号ごとにテキストを切り出し、参照グラフを構築する。

Usage:
    python src/article_parser.py
"""

import json
import re
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF

ROOT_DIR = Path(__file__).resolve().parent.parent
PDF_PATH = ROOT_DIR / "data" / "2400594-RR-Vol 1-E-A5.pdf"
GRAPH_PATH = ROOT_DIR / "data" / "graph" / "reference_graph.json"
ARTICLES_PATH = ROOT_DIR / "data" / "graph" / "articles.json"

# PDF page ranges (0-indexed)
def _detect_article_ranges(pdf_path: str) -> dict:
    """PDFからRRページマーカーを検出し、全Articleのページ範囲を自動検出する。"""
    doc = fitz.open(pdf_path)
    current_art = None
    art_ranges = {}
    for i in range(doc.page_count):
        text = doc[i].get_text()[:300]
        markers = re.findall(r'RR(\d+)-(\d+)', text)
        if markers:
            art_num = int(markers[0][0])
            if art_num != current_art:
                if current_art is not None:
                    art_ranges[current_art] = (art_ranges[current_art], i)
                current_art = art_num
                art_ranges[art_num] = i
    if current_art:
        art_ranges[current_art] = (art_ranges[current_art], doc.page_count)
    doc.close()
    return art_ranges


# 初回実行時に自動検出（PDFが存在する場合）
if PDF_PATH.exists():
    ARTICLE_RANGES = _detect_article_ranges(str(PDF_PATH))
else:
    # フォールバック（Streamlit Cloud等でPDFがない場合）
    ARTICLE_RANGES = {}

# 参照パターン（基本: "No. 9.1", "Nos. 9.11A"）
REF_PATTERN = re.compile(r'No[s]?\.\s*(\d+\.\d+[A-Z]?)')

# 列挙パターン: "No. 9.1 or 9.2", "Nos. 9.11A, 9.17 and 9.18", "Nos. 9.15 to 9.19"
# 基本パターンの後に続く ", X.X" "or X.X" "and X.X" "to X.X" もキャプチャ
REF_CONTINUATION = re.compile(r'(?:,\s*|\s+(?:or|and|to)\s+)(\d+\.\d+[A-Z]?)')


def extract_all_refs(text: str) -> list:
    """テキストからNo. X.X参照を全て抽出する（列挙パターン対応）。"""
    refs = []
    for m in REF_PATTERN.finditer(text):
        refs.append(m.group(1))
        # "No. 9.1 or 9.2" のような列挙の続きを探す
        rest = text[m.end():]
        while True:
            cm = REF_CONTINUATION.match(rest)
            if cm:
                refs.append(cm.group(1))
                rest = rest[cm.end():]
            else:
                break
    return refs


def sort_article_key(num: str) -> tuple:
    """条文番号を数値ソート。"""
    m = re.match(r'(\d+)\.(\d+)([A-Z]*)', num)
    if not m:
        return (999, 999, num)
    return (int(m.group(1)), int(m.group(2)), m.group(3))


def extract_footnotes(pdf_path: str, start_page: int, end_page: int) -> dict:
    """PDFから脚注を抽出し、親条文番号→脚注テキストリストの辞書を返す。

    脚注フォーマット: "23 11.44.1 テキスト..."
    → 親条文 "11.44" に紐付け
    """
    doc = fitz.open(pdf_path)
    footnotes = {}  # footnote_num -> text
    for page_num in range(start_page, end_page):
        page_text = doc[page_num].get_text()
        lines = page_text.split("\n")
        in_footnotes = False
        current_fn = None
        current_text = ""
        for line in lines:
            if "___" in line:
                in_footnotes = True
                continue
            if not in_footnotes:
                continue
            m = re.match(r'^(\d+)\s+(\d+\.\d+)', line.strip())
            if m:
                if current_fn:
                    footnotes[current_fn] = current_text.strip()
                current_fn = m.group(1)
                current_text = line.strip()
            elif current_fn and line.strip():
                current_text += " " + line.strip()
        if current_fn:
            footnotes[current_fn] = current_text.strip()
    doc.close()

    # 脚注を親条文番号に紐付け
    # "23 11.44.1 text..." → 親 = "11.44"
    parent_footnotes = defaultdict(list)
    fn_parent_pattern = re.compile(r'^\d+\s+(\d+\.\d+[A-Z]?)')
    for fn_num, text in footnotes.items():
        m = fn_parent_pattern.match(text)
        if m:
            parent = m.group(1)
            parent_footnotes[parent].append(text)

    return parent_footnotes


def extract_full_text(pdf_path: str, start_page: int, end_page: int) -> str:
    """PDFの指定ページ範囲からテキストを抽出する。"""
    doc = fitz.open(pdf_path)
    text = ""
    for i in range(start_page, end_page):
        page_text = doc[i].get_text()
        # ヘッダー/フッター除去（RR9-1, CHAPTER III...）
        lines = page_text.split("\n")
        cleaned = []
        for line in lines:
            # ページ番号パターン（– 209 –）
            if re.match(r'^–\s*\d+\s*–\s*$', line.strip()):
                continue
            # RRページマーカー（RR9-1, RR11-14）
            if re.match(r'^RR\d+-\d+\s*$', line.strip()):
                continue
            # CHAPTER IIIヘッダー
            if 'CHAPTER III' in line and 'Coordination' in line:
                continue
            cleaned.append(line)
        text += "\n".join(cleaned) + "\n"
    doc.close()
    return text


def parse_provisions(full_text: str, article_num: int) -> dict:
    """
    テキストから各条文を切り出す。

    Returns:
        {provision_number: text} e.g. {"9.12": "f) for a station..."}
    """
    prefix = f"{article_num}."

    # 条文番号の出現位置を検出
    # パターン: 条文番号が単独で行を構成するケースのみマッチ
    # ※段落中の折り返し「...or \n11.44, as...」を除外
    pattern = re.compile(
        rf'(?:^|\n)({article_num}\.\d+[A-Z]?)\s*(?:\n|$)',
    )

    matches = list(pattern.finditer(full_text))

    provisions = {}
    for i, match in enumerate(matches):
        prov_num = match.group(1)
        start = match.start()
        # テキストの先頭が改行なら飛ばす
        if full_text[start] == '\n':
            start += 1

        # 終了位置: 次の条文番号の開始位置
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(full_text)

        text = full_text[start:end].strip()

        # 脚注区切り線（___）以降を除外
        fn_sep = re.search(r'\n___+\n', text)
        if fn_sep:
            text = text[:fn_sep.start()].strip()

        # 同じ番号が複数回出現する場合は最初のものを使用
        # （ただしテキストが短すぎる場合は次を試す）
        if prov_num not in provisions or len(provisions[prov_num]) < 30:
            if len(text) > 10:
                provisions[prov_num] = text

    return provisions


CHUNKS_PATH = ROOT_DIR / "data" / "index" / "chunks.json"


def build_graph_combined(pdf_provisions: dict, pdf_footnotes: dict = None) -> tuple:
    """
    統合グラフ構築:
    - 条文テキスト: PDFから直接パース（Article 9・11のみ、正確）
    - 参照グラフ: 全チャンク（Vol.1〜4+RoP）からパース（網羅的）
      + PDFパース結果のArticle 9・11内参照も追加
      + 脚注内の参照も追加

    Returns:
        (graph, articles) の tuple
    """
    # ── 1. 全チャンクから参照関係を抽出 ──
    print("  Loading chunks for cross-reference extraction...")
    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    # refs_to_tagged: {source_num: {ref: source_type}} where source_type = "text" | "footnote"
    refs_to_tagged = defaultdict(dict)
    refs_from = defaultdict(set)
    chunk_texts = {}  # チャンクベースのテキスト（PDF未対応条文のフォールバック）

    def _is_valid_ref(ref: str, source_num: str) -> bool:
        """参照が有効かチェックする。"""
        if ref == source_num:
            return False
        parts = ref.split(".")
        if len(parts) == 2 and len(parts[1].rstrip("ABCDEFG")) > 3:
            return False
        return True

    for chunk in chunks:
        article_no = chunk.get("article_no", "").strip()
        text = chunk.get("text", "")
        vol = chunk.get("vol", "")

        m = re.match(r'No\.?\s*(\d+\.\d+[A-Z]?)', article_no)
        if not m:
            continue
        source_num = m.group(1)

        # チャンクテキストを保存（Vol.1優先）
        if source_num not in chunk_texts or vol == "Vol.1":
            chunk_texts[source_num] = {"text": text, "vol": vol,
                                       "section_path": chunk.get("section_path", "")}

        found_refs = extract_all_refs(text)
        for ref in found_refs:
            if not _is_valid_ref(ref, source_num):
                continue
            # PDFパース済み条文のrefs_to/refs_fromはチャンクから取らない
            # （チャンカーの誤タグ付けで他条文の参照が混入するため）
            if source_num not in pdf_provisions:
                if not ref.startswith("5."):
                    if ref not in refs_to_tagged[source_num]:
                        refs_to_tagged[source_num][ref] = "text"
                refs_from[ref].add(source_num)

    print(f"  Chunks: {len(chunks)} → {sum(len(v) for v in refs_to_tagged.values())} refs_to links")

    # ── 2. PDFパース結果からの参照も追加（本文） ──
    for source_num, text in pdf_provisions.items():
        found_refs = extract_all_refs(text)
        for ref in found_refs:
            if not _is_valid_ref(ref, source_num):
                continue
            if not ref.startswith("5."):
                if ref not in refs_to_tagged[source_num]:
                    refs_to_tagged[source_num][ref] = "text"
            refs_from[ref].add(source_num)

    # ── 3. 脚注内の参照も追加 ──
    if pdf_footnotes:
        fn_refs_count = 0
        for parent_num, fn_texts in pdf_footnotes.items():
            for fn_text in fn_texts:
                found_refs = extract_all_refs(fn_text)
                for ref in found_refs:
                    if not _is_valid_ref(ref, parent_num):
                        continue
                    if not ref.startswith("5."):
                        # 脚注由来: 本文で既にある場合は"text"のまま、新規なら"footnote"
                        if ref not in refs_to_tagged[parent_num]:
                            refs_to_tagged[parent_num][ref] = "footnote"
                        fn_refs_count += 1
                    refs_from[ref].add(parent_num)
        print(f"  Footnotes: {sum(len(v) for v in pdf_footnotes.values())} entries → {fn_refs_count} refs_to links added")

    # ── 4. 全条文番号を収集 ──
    all_nums = set(pdf_provisions.keys()) | set(chunk_texts.keys())
    for num in list(refs_to_tagged.keys()) + list(refs_from.keys()):
        refs_set = set(refs_to_tagged.get(num, {}).keys()) | refs_from.get(num, set())
        all_nums.update(refs_set)

    # ── 5. グラフ構築 ──
    graph = {}
    for num in sorted(all_nums, key=sort_article_key):
        tagged = refs_to_tagged.get(num, {})
        graph[num] = {
            "refs_to": sorted(tagged.keys(), key=sort_article_key),
            "refs_to_sources": {k: tagged[k] for k in sorted(tagged.keys(), key=sort_article_key)},
            "refs_from": sorted(refs_from.get(num, set()), key=sort_article_key),
        }

    # ── 6. 条文テキスト構築（PDF優先、チャンクフォールバック） ──
    articles = {}
    for num in sorted(all_nums, key=sort_article_key):
        if num in pdf_provisions and pdf_provisions[num]:
            # PDFから直接パースしたテキスト（Article 9・11）
            articles[num] = {
                "number": num,
                "text": pdf_provisions[num],
                "vol": "Vol.1",
                "section_path": f"ARTICLE {num.split('.')[0]}",
            }
        elif num in chunk_texts:
            # チャンクベースのテキスト（その他のArticle）
            ct = chunk_texts[num]
            articles[num] = {
                "number": num,
                "text": ct["text"],
                "vol": ct["vol"],
                "section_path": ct["section_path"],
            }
        else:
            articles[num] = {
                "number": num,
                "text": "",
                "vol": "",
                "section_path": "",
            }

    return graph, articles


def main():
    print("=" * 60)
    print("統合グラフ構築（PDF直接パース + 全チャンク参照）")
    print("=" * 60)

    # ── PDFからArticle 9・11の条文テキストをパース ──
    pdf_provisions = {}

    for article_num, (start_page, end_page) in ARTICLE_RANGES.items():
        print(f"\nArticle {article_num}: PDF pages {start_page}-{end_page - 1}")

        full_text = extract_full_text(str(PDF_PATH), start_page, end_page)
        print(f"  Extracted {len(full_text)} characters")

        provisions = parse_provisions(full_text, article_num)
        print(f"  Parsed {len(provisions)} provisions")

        for num in sorted(provisions.keys(), key=sort_article_key):
            text_preview = provisions[num][:80].replace('\n', ' ')
            print(f"    {num:10s} {text_preview}...")

        pdf_provisions.update(provisions)

    print(f"\nPDF provisions total: {len(pdf_provisions)}")

    # ── 脚注を抽出 ──
    pdf_footnotes = {}
    for article_num, (start_page, end_page) in ARTICLE_RANGES.items():
        fn = extract_footnotes(str(PDF_PATH), start_page, end_page)
        pdf_footnotes.update(fn)
        print(f"  Article {article_num} footnotes: {len(fn)} parent articles")

    # ── 統合グラフ構築 ──
    print("\nBuilding combined reference graph...")
    graph, articles = build_graph_combined(pdf_provisions, pdf_footnotes)

    # 保存
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(GRAPH_PATH, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    print(f"Reference graph saved: {GRAPH_PATH} ({len(graph)} articles)")

    with open(ARTICLES_PATH, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    print(f"Article texts saved: {ARTICLES_PATH} ({len(articles)} articles)")

    # 統計
    total_refs = sum(len(v["refs_to"]) for v in graph.values())
    art9 = [k for k in graph if k.startswith("9.")]
    art11 = [k for k in graph if k.startswith("11.")]
    print(f"\nStats:")
    print(f"  Total articles in graph: {len(graph)}")
    print(f"  Total reference links (refs_to): {total_refs}")
    print(f"  Article 9 provisions: {len(art9)}")
    print(f"  Article 11 provisions: {len(art11)}")

    # サンプル確認
    print(f"\nSample - No. 9.12:")
    print(f"  refs_to: {graph.get('9.12', {}).get('refs_to', [])}")
    print(f"  refs_from: {graph.get('9.12', {}).get('refs_from', [])}")
    print(f"  text[:200]: {articles.get('9.12', {}).get('text', '')[:200]}")


if __name__ == "__main__":
    main()
