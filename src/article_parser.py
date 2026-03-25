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
ARTICLE_RANGES = {
    9: (218, 232),   # Article 9: PDF pages 218-231
    11: (234, 248),  # Article 11: PDF pages 234-247
}

# 参照パターン
REF_PATTERN = re.compile(r'No[s]?\.\s*(\d+\.\d+[A-Z]?)')


def sort_article_key(num: str) -> tuple:
    """条文番号を数値ソート。"""
    m = re.match(r'(\d+)\.(\d+)([A-Z]*)', num)
    if not m:
        return (999, 999, num)
    return (int(m.group(1)), int(m.group(2)), m.group(3))


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
    # パターン: 行頭（またはインデント後）の "9.12" のような数字
    pattern = re.compile(
        rf'(?:^|\n)\s*({article_num}\.\d+[A-Z]?)\b',
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

        # 同じ番号が複数回出現する場合は最初のものを使用
        # （ただしテキストが短すぎる場合は次を試す）
        if prov_num not in provisions or len(provisions[prov_num]) < 30:
            if len(text) > 10:
                provisions[prov_num] = text

    return provisions


CHUNKS_PATH = ROOT_DIR / "data" / "index" / "chunks.json"


def build_graph_combined(pdf_provisions: dict) -> tuple:
    """
    統合グラフ構築:
    - 条文テキスト: PDFから直接パース（Article 9・11のみ、正確）
    - 参照グラフ: 全チャンク（Vol.1〜4+RoP）からパース（網羅的）
      + PDFパース結果のArticle 9・11内参照も追加

    Returns:
        (graph, articles) の tuple
    """
    # ── 1. 全チャンクから参照関係を抽出 ──
    print("  Loading chunks for cross-reference extraction...")
    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    refs_to = defaultdict(set)
    refs_from = defaultdict(set)
    chunk_texts = {}  # チャンクベースのテキスト（PDF未対応条文のフォールバック）

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

        found_refs = REF_PATTERN.findall(text)
        for ref in found_refs:
            if ref == source_num:
                continue
            parts = ref.split(".")
            if len(parts) == 2 and len(parts[1].rstrip("ABCDEFG")) > 3:
                continue
            if not ref.startswith("5."):
                refs_to[source_num].add(ref)
            refs_from[ref].add(source_num)

    print(f"  Chunks: {len(chunks)} → {sum(len(v) for v in refs_to.values())} refs_to links")

    # ── 2. PDFパース結果からの参照も追加 ──
    for source_num, text in pdf_provisions.items():
        found_refs = REF_PATTERN.findall(text)
        for ref in found_refs:
            if ref == source_num:
                continue
            parts = ref.split(".")
            if len(parts) == 2 and len(parts[1].rstrip("ABCDEFG")) > 3:
                continue
            if not ref.startswith("5."):
                refs_to[source_num].add(ref)
            refs_from[ref].add(source_num)

    # ── 3. 全条文番号を収集 ──
    all_nums = set(pdf_provisions.keys()) | set(chunk_texts.keys())
    for num in list(refs_to.keys()) + list(refs_from.keys()):
        for ref_set in [refs_to.get(num, set()), refs_from.get(num, set())]:
            all_nums.update(ref_set)

    # ── 4. グラフ構築 ──
    graph = {}
    for num in sorted(all_nums, key=sort_article_key):
        graph[num] = {
            "refs_to": sorted(refs_to.get(num, set()), key=sort_article_key),
            "refs_from": sorted(refs_from.get(num, set()), key=sort_article_key),
        }

    # ── 5. 条文テキスト構築（PDF優先、チャンクフォールバック） ──
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

    # ── 統合グラフ構築 ──
    print("\nBuilding combined reference graph...")
    graph, articles = build_graph_combined(pdf_provisions)

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
