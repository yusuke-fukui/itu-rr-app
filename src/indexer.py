"""
ITU無線通則（Radio Regulations）PDFからベクトルインデックスを作成するスクリプト。
PyMuPDFでテキスト抽出 → 条文番号で分割 → sentence-transformersでベクトル化 → FAISSに保存。
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

# プロジェクトルート
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
INDEX_DIR = DATA_DIR / "index"

# 対象PDFファイル（Vol.1〜4 + Rules of Procedure）
PDF_FILES = [
    "2400594-RR-Vol 1-E-A5.pdf",
    "2400594-RR-Vol 2-E-A5.pdf",
    "2400594-RR-Vol 3-E-A5.pdf",
    "2400594-RR-Vol 4-E-A5.pdf",
    "R-REG-ROP-2021-R02-PDF-E.pdf",
]

# 条文番号パターン（例: No. 1.1, No. 21.18A）
ARTICLE_PATTERN = re.compile(r"(No\.\s*\d+\.\d+[A-Z]?)")

# 埋め込みモデル
MODEL_NAME = "all-MiniLM-L6-v2"

# 条文番号が見つからない場合のフォールバックチャンクサイズ
FALLBACK_CHUNK_SIZE = 500

# --- 文書構造を検出するための正規表現 ---

# 印刷ページ番号（例: – 165 –, – IX –）
PRINTED_PAGE_PATTERN = re.compile(r"–\s*(\d+)\s*–")

# CHAPTER見出し（例: CHAPTER I  Terminology and technical characteristics）
CHAPTER_PATTERN = re.compile(
    r"^(CHAPTER\s+[IVX]+\s+.+?)$", re.MULTILINE
)

# ARTICLE見出し（例: ARTICLE 5, ARTICLE 22）
ARTICLE_HEADING_PATTERN = re.compile(
    r"^(ARTICLE\s+\d+[A-Z]?)\b", re.MULTILINE
)

# RESOLUTION見出し（例: RESOLUTION 123 (WRC-23)）
RESOLUTION_PATTERN = re.compile(
    r"^(RESOLUTION\s+\d+[A-Z]?\s*(?:\((?:Rev\.)?WRC-\d+\)|\(Rev\.\s*WRC-\d+\))?)", re.MULTILINE
)

# RECOMMENDATION見出し
RECOMMENDATION_PATTERN = re.compile(
    r"^(RECOMMENDATION\s+\d+[A-Z]?\s*(?:\((?:Rev\.)?WRC-\d+\))?)", re.MULTILINE
)

# ANNEX見出し（例: ANNEX 2 TO RESOLUTION 123 (WRC-23)）
ANNEX_PATTERN = re.compile(
    r"^(ANNEX\s+\d+[A-Z]?(?:\s+TO\s+RESOLUTION\s+\d+[A-Z]?\s*(?:\((?:Rev\.)?WRC-\d+\))?)?)", re.MULTILINE
)

# APPENDIX見出し
APPENDIX_PATTERN = re.compile(
    r"^(APPENDIX\s+\d+[A-Z]?(?:\s+TO\s+(?:RESOLUTION|ANNEX)\s+\d+[A-Z]?\s*(?:\((?:Rev\.)?WRC-\d+\))?)?)", re.MULTILINE
)


def extract_printed_page(text: str) -> str:
    """ページテキストから印刷ページ番号を抽出する。"""
    match = PRINTED_PAGE_PATTERN.search(text)
    return match.group(1) if match else ""


def _decode_font_shift(text: str) -> str:
    """
    PDFカスタムフォントエンコーディングによるシフト文字化けをデコードする。
    Vol.4のQ信号テーブル等で使われるフォントが文字コードをシフトして保存している。
    - 英字(A-Z, a-z): ROT-3デコード（-3シフト）
    - 非英字の印刷可能ASCII: +29シフト
    """
    result = []
    for c in text:
        code = ord(c)
        if 'A' <= c <= 'Z':
            result.append(chr((code - ord('A') - 3) % 26 + ord('A')))
        elif 'a' <= c <= 'z':
            result.append(chr((code - ord('a') - 3) % 26 + ord('a')))
        elif 33 <= code <= 64 or 91 <= code <= 96 or 123 <= code <= 126:
            # 非英字の印刷可能ASCII
            decoded = code + 29
            if 32 <= decoded <= 126:
                result.append(chr(decoded))
            else:
                result.append(c)
        else:
            result.append(c)
    return ''.join(result)


def _vowel_ratio(text: str, min_len: int = 4) -> float:
    """テキストの母音比率を計算する。正常な英語は0.35-0.45程度。"""
    alpha = re.sub(r'[^a-zA-Z]', '', text)
    if len(alpha) < min_len:
        return 0.4  # 短すぎるテキストはデフォルト値
    vowels = sum(1 for c in alpha.lower() if c in 'aeiou')
    return vowels / len(alpha)


def fix_font_encoding(text: str) -> str:
    """
    フォントエンコーディング文字化けを検出し、可能なら修復する。
    行単位でデコード判定を行い、正常テキストと文字化けの混在にも対応する。
    """
    alpha_only = re.sub(r'[^a-zA-Z]', '', text)
    if len(alpha_only) < 8:
        return text  # 英字が少ないテキストはスキップ

    # 常に行単位でデコード判定（正常テキストと文字化けの混在に対応）（正常テキストと文字化けの混在に対応）
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


def clean_control_chars(s: str) -> str:
    """制御文字・私用領域文字・数式フォント由来の誤抽出文字を除去する。"""
    # C0制御文字（\x00-\x1f）をスペースに置換（\n, \t は保持）
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s)
    # C1制御文字（\x80-\x9f）を除去
    s = re.sub(r"[\x80-\x9f]", "", s)
    # Unicode私用領域（U+E000-F8FF）を除去 — PDFのSymbolフォント由来
    s = re.sub(r"[\ue000-\uf8ff]", "", s)
    # PDFの数式フォントから誤抽出されたスクリプト文字を除去
    # （ITU-RRは英語文書なので、これらのスクリプトは本来含まれない）
    s = re.sub(
        r"["
        r"\u0600-\u06FF"    # アラビア文字
        r"\u0700-\u077F"    # シリア文字+補助
        r"\u0780-\u07BF"    # ターナ文字
        r"\u0900-\u097F"    # デーヴァナーガリー
        r"\u0980-\u09FF"    # ベンガル文字
        r"\u0A00-\u0A7F"    # グルムキー文字
        r"\u0A80-\u0AFF"    # グジャラート文字
        r"\u0B00-\u0B7F"    # オリヤー文字
        r"\u0B80-\u0BFF"    # タミル文字
        r"\u0C00-\u0C7F"    # テルグ文字
        r"\u0C80-\u0CFF"    # カンナダ文字
        r"\u0D00-\u0D7F"    # マラヤーラム文字
        r"\u0D80-\u0DFF"    # シンハラ文字
        r"\u1200-\u137F"    # エチオピア文字
        r"\u0590-\u05FF"    # ヘブライ文字
        r"\u0800-\u083F"    # サマリア文字
        r"\u0F00-\u0FFF"    # チベット文字
        r"\u1B80-\u1BBF"    # スンダ文字
        r"\uFB00-\uFDFF"    # アラビア表示形A
        r"\uFE70-\uFEFF"    # アラビア表示形B
        r"]+", "", s
    )
    # Unicode置換文字を除去
    s = re.sub(r"[\ufffd\ufffe\uffff]", "", s)
    # 連続する空白を1つに
    s = re.sub(r"[ \t]+", " ", s)
    s = s.strip()
    # フォントエンコーディング文字化けの修復
    s = fix_font_encoding(s)
    return s


def detect_section_headings(text: str) -> dict:
    """
    ページテキストから文書構造の見出しを検出する。
    最も具体的な（最後に出現する）見出しを返す。
    """
    headings = {}

    # CHAPTER
    m = CHAPTER_PATTERN.search(text)
    if m:
        headings["chapter"] = clean_control_chars(m.group(1))

    # ARTICLE
    m = ARTICLE_HEADING_PATTERN.search(text)
    if m:
        headings["article"] = clean_control_chars(m.group(1))

    # RESOLUTION
    m = RESOLUTION_PATTERN.search(text)
    if m:
        headings["resolution"] = clean_control_chars(m.group(1))

    # RECOMMENDATION
    m = RECOMMENDATION_PATTERN.search(text)
    if m:
        headings["recommendation"] = clean_control_chars(m.group(1))

    # ANNEX（RESOLUTION内のANNEX）
    m = ANNEX_PATTERN.search(text)
    if m:
        headings["annex"] = clean_control_chars(m.group(1))

    # APPENDIX
    m = APPENDIX_PATTERN.search(text)
    if m:
        headings["appendix"] = clean_control_chars(m.group(1))

    return headings


def detect_front_matter(text: str) -> str:
    """前付ページ（表紙・免責事項・序文・目次等）のセクション名を推定する。"""
    text_lower = text.lower()

    # 具体的なものから先に判定（表紙は最後＝フォールバック）
    if "disclaimer" in text_lower:
        return "免責事項 (Disclaimer)"
    if "note by the secretariat" in text_lower:
        return "事務局注記 (Note by the Secretariat)"
    if "preamble" in text_lower:
        return "前文 (Preamble)"
    if "table of contents" in text_lower:
        return "目次 (Table of Contents)"

    # 略語一覧
    if "abbreviation" in text_lower and "conference" in text_lower:
        return "略語一覧 (Abbreviations)"

    # VOLUMEページ（目次の始まり）
    vol_match = re.search(r"VOLUME\s+\d+", text)
    if vol_match:
        return "目次 (Table of Contents)"

    # 上記に該当しなければ表紙
    if "radio regulations" in text_lower:
        return "表紙 (Cover)"

    return ""


def build_section_path(headings: dict) -> str:
    """
    検出された見出し情報からセクションパス（ツリー）を構築する。
    例: "RESOLUTION 123 (WRC-23) > ANNEX 2"
    例: "CHAPTER II > ARTICLE 5"
    """
    parts = []

    # 前付ページ
    if "front_matter" in headings:
        return headings["front_matter"]

    # 優先順位: RESOLUTION/RECOMMENDATION > ANNEX > APPENDIX > CHAPTER > ARTICLE
    if "resolution" in headings:
        parts.append(headings["resolution"])
    elif "recommendation" in headings:
        parts.append(headings["recommendation"])

    if "annex" in headings:
        annex_text = headings["annex"]
        # "ANNEX 2 TO RESOLUTION 123" の場合、RESOLUTION部分は親に含まれるので省略
        if "TO RESOLUTION" in annex_text and parts:
            # ANNEX番号だけ抽出
            annex_num = re.match(r"ANNEX\s+\d+[A-Z]?", annex_text)
            if annex_num:
                parts.append(annex_num.group())
        else:
            parts.append(annex_text)

    if "appendix" in headings:
        appendix_text = headings["appendix"]
        if "TO" in appendix_text and parts:
            app_num = re.match(r"APPENDIX\s+\d+[A-Z]?", appendix_text)
            if app_num:
                parts.append(app_num.group())
        else:
            parts.append(appendix_text)

    if "chapter" in headings:
        parts.append(headings["chapter"])

    if "article" in headings:
        parts.append(headings["article"])

    return " > ".join(parts)


def extract_text_from_pdf(pdf_path: Path) -> list[dict]:
    """PDFからページごとにテキストを抽出し、印刷ページ番号と構造情報を付与する。"""
    pages = []
    doc = fitz.open(str(pdf_path))

    # 文書構造の追跡（ページをまたいで継続）
    current_context = {}

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        if not text.strip():
            continue

        # テキストから制御文字を除去（見出し検出前に実施）
        text = clean_control_chars(text)

        # 印刷ページ番号を抽出
        printed_page = extract_printed_page(text)

        # このページの見出しを検出
        page_headings = detect_section_headings(text)

        if page_headings:
            # 正式な見出しが見つかった → front_matterモードを終了
            current_context.pop("front_matter", None)

            # コンテキストを更新（新しい見出しがあれば上書き）
            # 上位の見出しが変わったら下位をリセット
            if "resolution" in page_headings or "recommendation" in page_headings:
                current_context.pop("annex", None)
                current_context.pop("appendix", None)
            if "chapter" in page_headings:
                current_context.pop("article", None)

            current_context.update(page_headings)
        elif not current_context or "front_matter" in current_context:
            # まだ正式な見出しが出ていない前付ページ → 毎ページ再判定
            front_section = detect_front_matter(text)
            if front_section:
                current_context["front_matter"] = front_section

        # セクションパスを構築
        section_path = build_section_path(current_context)

        pages.append({
            "pdf_page": page_num + 1,
            "printed_page": printed_page,
            "section_path": section_path,
            "text": text,
        })

    doc.close()
    return pages


def split_by_sentences(text: str, max_chunk_size: int = 500) -> list[str]:
    """テキストを文の区切りで分割する。文の途中で切れないようにする。"""
    # 文の区切りパターン: ピリオド+スペース、改行2つ以上
    sentences = re.split(r"(?<=[.;:])\s+|\n{2,}", text)

    result_chunks = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        # 現在のチャンクに追加しても上限以内ならマージ
        if current and len(current) + len(sent) + 1 > max_chunk_size:
            result_chunks.append(current)
            current = sent
        else:
            current = f"{current} {sent}".strip() if current else sent
    if current:
        result_chunks.append(current)

    return result_chunks


# 短すぎるチャンクの最小サイズ（これ未満は前のチャンクに結合）
MIN_CHUNK_SIZE = 80


def split_into_chunks(pages: list[dict], vol: str) -> list[dict]:
    """ページテキストを条文番号で分割してチャンクを作成する。"""
    chunks = []

    for page_info in pages:
        text = page_info["text"]
        pdf_page = page_info["pdf_page"]
        printed_page = page_info["printed_page"]
        section_path = page_info["section_path"]

        base_meta = {
            "vol": vol,
            "pdf_page": pdf_page,
            "printed_page": printed_page,
            "section_path": section_path,
        }

        # 条文番号で分割を試みる
        parts = ARTICLE_PATTERN.split(text)

        if len(parts) > 1:
            # 条文番号が見つかった場合
            if parts[0].strip():
                chunks.append({
                    **base_meta,
                    "article_no": "",
                    "text": parts[0].strip(),
                })

            for i in range(1, len(parts), 2):
                article_no = parts[i].strip()
                body = parts[i + 1].strip() if i + 1 < len(parts) else ""
                combined = f"{article_no} {body}"
                if combined.strip():
                    chunks.append({
                        **base_meta,
                        "article_no": article_no,
                        "text": combined,
                    })
        else:
            # 条文番号が見つからない場合：文の区切りで分割
            sub_chunks = split_by_sentences(text, FALLBACK_CHUNK_SIZE)
            for chunk_text in sub_chunks:
                chunk_text = chunk_text.strip()
                if chunk_text:
                    chunks.append({
                        **base_meta,
                        "article_no": "",
                        "text": chunk_text,
                    })

    # 短すぎるチャンクを前のチャンクに結合（ページ跨ぎも許容）
    merged = []
    for chunk in chunks:
        if (merged
            and len(chunk["text"]) < MIN_CHUNK_SIZE
            and merged[-1]["vol"] == chunk["vol"]):
            merged[-1]["text"] += " " + chunk["text"]
        else:
            merged.append(chunk)

    return merged


def build_index(force: bool = False):
    """全PDFからベクトルインデックスを構築する。"""

    # 既にインデックスが存在する場合
    if not force and (INDEX_DIR / "faiss.index").exists() and (INDEX_DIR / "chunks.json").exists():
        print("✓ インデックスは既に存在します。再作成するには --force オプションを使用してください。")
        return

    # PDFファイルの存在確認
    missing = []
    for pdf_name in PDF_FILES:
        if not (DATA_DIR / pdf_name).exists():
            missing.append(pdf_name)
    if missing:
        print("エラー: 以下のPDFファイルが data/ フォルダに見つかりません:")
        for f in missing:
            print(f"  - {f}")
        print("\nPDFファイルを data/ フォルダにコピーしてください。")
        sys.exit(1)

    # テキスト抽出とチャンク分割
    print("📄 PDFからテキストを抽出中...")
    all_chunks = []
    for pdf_name in tqdm(PDF_FILES, desc="PDF読み込み"):
        pdf_path = DATA_DIR / pdf_name
        # ファイル名からVol番号を抽出
        vol_match = re.search(r"Vol[_ ](\d+)", pdf_name)
        if vol_match:
            vol = f"Vol.{vol_match.group(1)}"
        elif "ROP" in pdf_name:
            vol = "RoP"
        else:
            vol = pdf_name

        pages = extract_text_from_pdf(pdf_path)
        chunks = split_into_chunks(pages, vol)
        all_chunks.extend(chunks)

    print(f"✓ {len(all_chunks)}件のチャンクを作成しました。")

    if not all_chunks:
        print("エラー: チャンクが作成されませんでした。PDFファイルを確認してください。")
        sys.exit(1)

    # ベクトル化
    print(f"🔢 ベクトル化中（モデル: {MODEL_NAME}）...")
    model = SentenceTransformer(MODEL_NAME)
    texts = [chunk["text"] for chunk in all_chunks]

    # バッチでエンコード（進捗表示付き）
    embeddings = model.encode(
        texts,
        show_progress_bar=True,
        batch_size=64,
        normalize_embeddings=True,
    )

    # FAISSインデックス作成
    print("📦 FAISSインデックスを作成中...")
    dimension = embeddings.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(embeddings.astype(np.float32))

    # 保存
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_DIR / "faiss.index"))

    with open(INDEX_DIR / "chunks.json", "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    print(f"✓ インデックスを保存しました: {INDEX_DIR}")
    print(f"  - faiss.index: {len(all_chunks)}件のベクトル（{dimension}次元）")
    print(f"  - chunks.json: メタデータ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ITU-RR PDFからベクトルインデックスを作成")
    parser.add_argument("--force", action="store_true", help="既存インデックスを上書きして再作成")
    args = parser.parse_args()

    build_index(force=args.force)
