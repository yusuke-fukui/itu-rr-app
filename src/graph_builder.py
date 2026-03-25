"""
条文参照グラフ構築スクリプト。
全チャンクから明示的な No. X.X 参照をパースし、双方向グラフ構造をJSONに保存する。

Usage:
    python src/graph_builder.py
"""

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CHUNKS_PATH = ROOT_DIR / "data" / "index" / "chunks.json"
GRAPH_PATH = ROOT_DIR / "data" / "graph" / "reference_graph.json"
ARTICLES_PATH = ROOT_DIR / "data" / "graph" / "articles.json"

# 参照パターン: No. 9.3, Nos. 9.12, No.11.31 等
REF_PATTERN = re.compile(r'No[s]?\.\s*(\d+\.\d+[A-Z]?)')


def sort_article_key(num: str) -> tuple:
    """条文番号を数値ソート用のタプルに変換する。
    "9.4" → (9, 4.0, ""), "9.11A" → (9, 11.0, "A"), "11.31" → (11, 31.0, "")
    """
    m = re.match(r'(\d+)\.(\d+)([A-Z]*)', num)
    if not m:
        return (999, 999, num)
    return (int(m.group(1)), int(m.group(2)), m.group(3))


def extract_article_text(chunks: list, target_num: str) -> dict:
    """
    条文番号に対応するテキストを全チャンクから抽出する。
    Vol.1の条文定義を最優先で返す。

    Returns:
        {"number": "9.12", "text": "...", "vol": "Vol.1", "section_path": "..."}
    """
    vol_priority = {"Vol.1": 0, "Vol.2": 1, "Vol.3": 2, "Vol.4": 3, "RoP": 4}
    candidates = []

    for chunk in chunks:
        article_no = chunk.get("article_no", "").strip()
        text = chunk.get("text", "")
        vol = chunk.get("vol", "")

        # article_no正規化
        m = re.match(r'No\.?\s*(\d+\.\d+[A-Z]?)', article_no)
        chunk_num = m.group(1) if m else ""

        if chunk_num == target_num:
            priority = vol_priority.get(vol, 9)
            candidates.append({
                "number": target_num,
                "text": text,
                "vol": vol,
                "section_path": chunk.get("section_path", ""),
                "priority": priority,
            })

    # Vol.1のチャンクテキスト内から条文定義を切り出す試み
    for chunk in chunks:
        if chunk.get("vol") != "Vol.1":
            continue
        text = chunk.get("text", "")
        if target_num not in text:
            continue

        # 条文定義の切り出し
        base_num = target_num.split('.')[0]
        patterns = [
            rf'(?:^|\n)\s*{re.escape(target_num)}\s*\n',
            rf'(?:^|\n)\s*No\.?\s*{re.escape(target_num)}\s',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                start = match.start()
                if text[start] == '\n':
                    start += 1
                # 次の条文番号で終了
                next_pattern = rf'\n\s*(?:{re.escape(base_num)}\.\d+[A-Z]?\b|No\.?\s*{re.escape(base_num)}\.\d+[A-Z]?\b)'
                rest = text[match.end():]
                next_m = re.search(next_pattern, rest)
                end = match.end() + next_m.start() if next_m else min(match.end() + 2000, len(text))
                extracted = text[start:end].strip()
                if len(extracted) > 20:
                    candidates.append({
                        "number": target_num,
                        "text": extracted,
                        "vol": "Vol.1",
                        "section_path": chunk.get("section_path", ""),
                        "priority": -1,  # インライン抽出は最優先
                    })

    if not candidates:
        return {"number": target_num, "text": "", "vol": "", "section_path": ""}

    candidates.sort(key=lambda x: x["priority"])
    best = candidates[0]
    best.pop("priority", None)
    return best


def build_graph():
    """全チャンクから参照グラフを構築する。"""
    print("Loading chunks...")
    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    print(f"  {len(chunks)} chunks loaded")

    # 参照関係の抽出
    print("Extracting references...")
    refs_to = defaultdict(set)   # source → {targets}
    refs_from = defaultdict(set)  # target → {sources}
    all_nums = set()

    for chunk in chunks:
        article_no = chunk.get("article_no", "").strip()
        text = chunk.get("text", "")

        # ソース条文番号
        m = re.match(r'No\.?\s*(\d+\.\d+[A-Z]?)', article_no)
        if not m:
            continue
        source_num = m.group(1)
        all_nums.add(source_num)

        # テキスト内の参照先
        found_refs = REF_PATTERN.findall(text)
        for ref in found_refs:
            if ref == source_num:
                continue  # 自己参照除外
            # 誤パース除外（9.712等の4桁以上の小数部）
            parts = ref.split(".")
            if len(parts) == 2 and len(parts[1].rstrip("ABCDEFG")) > 3:
                continue

            all_nums.add(ref)

            # 参照先（refs_to）から5条を除外
            # 5条は周波数分配表の脚注で数が多くノイズになる
            # ただし参照元（refs_from）には5条を含める
            if not ref.startswith("5."):
                refs_to[source_num].add(ref)
            # refs_from: 5条のノードの参照元にsourceを記録する（5条を含めてOK）
            refs_from[ref].add(source_num)

    # グラフJSON構築
    graph = {}
    for num in sorted(all_nums, key=sort_article_key):
        graph[num] = {
            "refs_to": sorted(refs_to.get(num, set()), key=sort_article_key),
            "refs_from": sorted(refs_from.get(num, set()), key=sort_article_key),
        }

    # 条文テキスト抽出
    print("Extracting article texts...")
    articles = {}
    for num in sorted(all_nums, key=sort_article_key):
        art = extract_article_text(chunks, num)
        articles[num] = art

    # 保存
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(GRAPH_PATH, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    print(f"  Reference graph saved: {GRAPH_PATH} ({len(graph)} articles)")

    with open(ARTICLES_PATH, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    print(f"  Article texts saved: {ARTICLES_PATH} ({len(articles)} articles)")

    # 統計
    total_refs = sum(len(v["refs_to"]) for v in graph.values())
    art9 = [k for k in graph if k.startswith("9.")]
    art11 = [k for k in graph if k.startswith("11.")]
    print(f"\nStats:")
    print(f"  Total articles: {len(graph)}")
    print(f"  Total reference links: {total_refs}")
    print(f"  Article 9: {len(art9)} provisions")
    print(f"  Article 11: {len(art11)} provisions")


if __name__ == "__main__":
    build_graph()
