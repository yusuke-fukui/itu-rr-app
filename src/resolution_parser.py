"""
Vol.3 PDF から決議テキストを構造化抽出するスクリプト。
各決議を preamble / resolves / further_resolves / instructs セクションに分割し、
印刷ページ番号を付与して JSON を更新する。
"""

import json
import re
from pathlib import Path

import fitz  # PyMuPDF

ROOT_DIR = Path(__file__).resolve().parent.parent
VOL3_PDF = ROOT_DIR / "data" / "2400594-RR-Vol 3-E-A5.pdf"
RESOLUTIONS_JSON = ROOT_DIR / "data" / "graph" / "vol3_resolutions_draft.json"

# 印刷ページ番号パターン（例: – 79 –）
PRINTED_PAGE_PATTERN = re.compile(r"–\s*(\d+)\s*–")

# ANNEX の開始を検出（ここ以降はスキップ）
ANNEX_PATTERN = re.compile(
    r"^ANNEX\s+\d+", re.MULTILINE
)

# 決議本文のセクションキーワード（出現順に並ぶ想定）
# resolves系キーワード（本文の核心部分）
RESOLVES_KEYWORDS = [
    "resolves",
    "further resolves",
    "decides",
    "further decides",
    "instructs the Secretary-General",
    "instructs the Director of the Radiocommunication Bureau",
    "instructs the Director",
    "invites administrations",
    "invites the Council",
    "invites Member States",
    "invites",
    "requests the Secretary-General",
    "requests the Director of the Radiocommunication Bureau",
    "requests the Director",
    "requests",
    "urges administrations",
    "urges",
    "encourages administrations",
    "encourages",
]

# preamble系キーワード
PREAMBLE_KEYWORDS = [
    "considering",
    "considering further",
    "noting",
    "noting further",
    "recognizing",
    "recognizing further",
    "referring to",
    "recalling",
    "having considered",
    "having noted",
    "having examined",
    "bearing in mind",
    "taking into account",
    "aware",
    "concerned",
    "convinced",
    "emphasizing",
    "reaffirming",
    "welcoming",
]


def extract_printed_page(text: str) -> str | None:
    """ページテキストから印刷ページ番号を抽出する。"""
    match = PRINTED_PAGE_PATTERN.search(text)
    return match.group(1) if match else None


def get_resolution_text(doc: fitz.Document, start_page: int, end_page: int) -> tuple[str, str | None, str | None]:
    """
    PDFの指定ページ範囲から決議テキストを取得。
    ANNEX以降を除外し、印刷ページ番号も返す。

    Returns:
        (text, printed_start, printed_end)
    """
    pages_text = []
    printed_start = None
    printed_end = None

    for page_idx in range(start_page - 1, min(end_page, len(doc))):
        page = doc[page_idx]
        text = page.get_text()

        # 印刷ページ番号を抽出
        pp = extract_printed_page(text)
        if pp:
            if printed_start is None:
                printed_start = pp
            printed_end = pp

        pages_text.append(text)

    full_text = "\n".join(pages_text)

    # ANNEX以降を除外
    annex_match = ANNEX_PATTERN.search(full_text)
    if annex_match:
        full_text = full_text[:annex_match.start()].rstrip()

    return full_text, printed_start, printed_end


def _build_section_pattern() -> re.Pattern:
    """セクションキーワードを検出する正規表現を構築。"""
    # resolves系とpreamble系を統合し、長い順にソート（longest match first）
    all_keywords = RESOLVES_KEYWORDS + PREAMBLE_KEYWORDS
    all_keywords_sorted = sorted(all_keywords, key=len, reverse=True)

    # 各キーワードをエスケープ
    escaped = [re.escape(kw) for kw in all_keywords_sorted]
    pattern = r"^\s*(" + "|".join(escaped) + r")\s*$"
    return re.compile(pattern, re.MULTILINE | re.IGNORECASE)


def _reflow_text(text: str) -> str:
    """
    PDFから抽出したテキストの改行を論理的な段落区切りに整形する。

    PDFは固定幅で改行するため、論理的な段落区切りとは無関係な位置で改行が入る。
    - 連続する行をスペースで結合（段落内の改行を除去）
    - 空行（\n\n）は段落区切りとして保持
    - セクションキーワード行は独立した行として保持
    - サブ項目（a), b) 等）の前で改行を保持
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    current_paragraph: list[str] = []

    # サブ項目パターン: a), b), 1), 2), etc.
    sub_item_pattern = re.compile(r"^[a-z]\)|\d+\.\d+|\d+\s+[A-Z]")
    # セクションキーワードパターン（行頭）
    section_kw_pattern = re.compile(
        r"^(considering|noting|recognizing|referring|recalling|having|"
        r"bearing|taking|aware|concerned|convinced|emphasizing|"
        r"reaffirming|welcoming|resolves?|further\s+resolves?|"
        r"decides?|instructs?|requests?|invites?|urges?|encourages?)",
        re.IGNORECASE
    )

    def flush_paragraph():
        if current_paragraph:
            result_lines.append(" ".join(current_paragraph))
            current_paragraph.clear()

    for line in lines:
        stripped = line.strip()

        # 空行 → 段落区切り
        if not stripped:
            flush_paragraph()
            continue

        # セクションキーワード行 → 独立行
        if section_kw_pattern.match(stripped):
            flush_paragraph()
            current_paragraph.append(stripped)
            flush_paragraph()
            continue

        # サブ項目 → 新しい段落として開始
        if sub_item_pattern.match(stripped):
            flush_paragraph()
            current_paragraph.append(stripped)
            continue

        # 通常行 → 現在の段落に結合
        current_paragraph.append(stripped)

    flush_paragraph()
    return "\n".join(result_lines)


def _normalize_keyword(keyword: str) -> str:
    """キーワードを正規化（小文字化・余白除去）。"""
    return keyword.strip().lower()


def _is_preamble_keyword(keyword: str) -> bool:
    """preamble系キーワードかどうかを判定。"""
    norm = _normalize_keyword(keyword)
    return any(norm == pk for pk in PREAMBLE_KEYWORDS)


def parse_sections(text: str) -> dict[str, str]:
    """
    決議テキストをセクションに分割する。

    Returns:
        {
            "preamble": "considering\na) ...\n\nconsidering further\n...",
            "resolves": "that the administrative due diligence...",
            "further_resolves": "...",
            "instructs": "...",
        }
    """
    section_pattern = _build_section_pattern()

    # セクションの開始位置を特定
    matches = list(section_pattern.finditer(text))
    if not matches:
        return {}

    # セクションごとにテキストを切り出し
    raw_sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        keyword = _normalize_keyword(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        raw_sections.append((keyword, section_text))

    # preamble系をまとめ、resolves系は個別に
    preamble_parts = []
    result: dict[str, str] = {}

    for keyword, section_text in raw_sections:
        # テキスト整形
        section_text = _reflow_text(section_text)

        if _is_preamble_keyword(keyword):
            preamble_parts.append(section_text)
        else:
            # スペースをアンダースコアに変換してキー化
            key = keyword.replace(" ", "_")
            # "instructs the director..." 系は "instructs" に統合
            if key.startswith("instructs"):
                key = "instructs"
            if key.startswith("requests"):
                key = "requests"
            if key.startswith("invites"):
                key = "invites"
            if key.startswith("urges"):
                key = "urges"
            if key.startswith("encourages"):
                key = "encourages"
            # 同じキーが複数回ある場合は結合
            if key in result:
                result[key] = result[key] + "\n\n" + section_text
            else:
                result[key] = section_text

    if preamble_parts:
        result["preamble"] = "\n\n".join(preamble_parts)

    return result


def extract_title(text: str) -> str | None:
    """
    決議テキストからタイトルを抽出する。
    RESOLUTION XX (WRC-YY) の直後〜 "The World Radiocommunication Conference" の前がタイトル。
    """
    # RESOLUTION ヘッダーを探す
    header_match = re.search(
        r"RESOLUTION\s+\d+[A-Z]?\s*\((?:Rev\.?|REV\.?)?(?:\s*WRC-\d+|ORB-\d+)\)",
        text, re.IGNORECASE
    )
    if not header_match:
        return None

    after_header = text[header_match.end():].lstrip()

    # "The World Radiocommunication Conference" を探す
    conf_match = re.search(
        r"The World Radiocommunication Conference",
        after_header
    )
    if not conf_match:
        # フォールバック: 最初のセクションキーワードの前
        section_pattern = _build_section_pattern()
        sm = section_pattern.search(after_header)
        if sm:
            title = after_header[:sm.start()].strip()
        else:
            return None
    else:
        title = after_header[:conf_match.start()].strip()

    # 改行をスペースに、連続スペースを1つに
    title = re.sub(r"\s+", " ", title).strip()
    # 末尾の脚注番号を除去（例: "...services1" → "...services"）
    title = re.sub(r"\d+$", "", title).strip()

    return title if title else None


def main():
    """メイン処理: PDF → 構造化JSON更新。"""
    with open(RESOLUTIONS_JSON, encoding="utf-8") as f:
        resolutions = json.load(f)

    doc = fitz.open(str(VOL3_PDF))
    updated = 0
    errors = []

    for res in resolutions:
        try:
            text, printed_start, printed_end = get_resolution_text(
                doc, res["start_page"], res["end_page"]
            )

            # 印刷ページ番号を追加
            if printed_start:
                res["printed_start_page"] = int(printed_start)
            if printed_end:
                res["printed_end_page"] = int(printed_end)

            # タイトル修正
            title = extract_title(text)
            if title:
                res["title"] = title

            # セクション分割
            sections = parse_sections(text)
            if sections:
                res["sections"] = sections
                updated += 1
            else:
                errors.append(f"Resolution {res['number']}: セクション抽出失敗")

        except Exception as e:
            errors.append(f"Resolution {res['number']}: {e}")

    doc.close()

    # JSON保存
    with open(RESOLUTIONS_JSON, "w", encoding="utf-8") as f:
        json.dump(resolutions, f, ensure_ascii=False, indent=2)

    print(f"完了: {updated}/{len(resolutions)} 件の決議を構造化")
    if errors:
        print(f"\nエラー ({len(errors)} 件):")
        for err in errors:
            print(f"  - {err}")


if __name__ == "__main__":
    main()
