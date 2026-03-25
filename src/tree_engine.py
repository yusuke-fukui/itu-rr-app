"""
インタラクティブ関連条文ツリーのエンジン。
Claude Sonnetを使って関連条文を推論し、ツリー構造を管理する。
"""

import json
import os
import re
import uuid
from typing import Optional

import anthropic
from dotenv import load_dotenv
from pathlib import Path

# プロジェクトルート
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

# Claude Sonnet モデル
TREE_MODEL = "claude-sonnet-4-20250514"

# 知識ベース読み込み
KNOWLEDGE_PATH = ROOT_DIR / "data" / "knowledge" / "article_relationships.json"


def _load_knowledge_context() -> str:
    """知識ベースJSONからシステムプロンプト用のコンテキストを生成する。"""
    if not KNOWLEDGE_PATH.exists():
        return ""

    try:
        with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            kb = json.load(f)

        lines = ["\n\n## ITU RR条文間の関係（知識ベース）\n"]

        # 規制手続きフロー
        flow = kb.get("regulatory_flow", {})
        if flow:
            lines.append("### 規制手続きフロー")
            for phase in flow.get("phases", []):
                arts = ", ".join(phase.get("key_articles", []))
                lines.append(f"- {phase['phase']}: {phase['description']} [{arts}]")

        # Art.9調整種別
        coord = kb.get("article_9_coordination_types", {})
        for prov, info in coord.get("provisions", {}).items():
            related = ", ".join(info.get("related", []))
            lines.append(f"- {prov}: {info['description']} → 関連: {related}")

        # Art.11審査
        exam = kb.get("article_11_examination", {})
        for key, info in exam.items():
            if isinstance(info, dict) and "description" in info:
                lines.append(f"- {key}: {info['description']}")

        # 重要な相互参照
        xref = kb.get("key_cross_references", {})
        for link in xref.get("links", []):
            if link.get("importance") in ("critical", "high"):
                to_str = ", ".join(link["to"]) if isinstance(link["to"], list) else link["to"]
                lines.append(f"- {link['from']} → {to_str}: {link['relationship']}")

        return "\n".join(lines)
    except Exception:
        return ""


# システムプロンプト
_KNOWLEDGE_CONTEXT = _load_knowledge_context()

SYSTEM_PROMPT = f"""あなたはITU Radio Regulations（無線通信規則）の専門家です。
与えられた条文に対して、概念的・手続き的に関連する他の条文を特定してください。
関連条文は以下の観点で選んでください：
- 同じ手続きや調整プロセスに関わる条文
- 同じ周波数帯・業務に適用される条文
- 定義や原則として参照される条文
- 派生する義務や権利を定める条文
{_KNOWLEDGE_CONTEXT}

出力はJSON形式で：
{{
  "summary": "この条文の1〜2行の要約（日本語）",
  "related": [
    {{
      "number": "No.9.21",
      "reason": "関連する理由（日本語・1行）"
    }}
  ]
}}
関連条文は最大5件までに絞り、関連度の高い順に並べること。
JSONのみを返し、前置きや説明は不要。"""


def generate_node_id() -> str:
    """ユニークなノードIDを生成する。"""
    return str(uuid.uuid4())[:8]


def _extract_article_from_chunk(text: str, number_part: str) -> Optional[str]:
    """
    大きなチャンクのテキスト内から、特定の条文番号で始まるセクションを切り出す。
    例: number_part="9.11" → "9.11\nd)\nfor a space station..." の部分を抽出。
    """
    # 条文番号のパターンを検索（行頭の "9.11" や "No. 9.11"）
    patterns = [
        # "9.11\n" や "9.11 " で始まる行を探す
        rf'(?:^|\n)\s*{re.escape(number_part)}\s*\n',
        rf'(?:^|\n)\s*No\.?\s*{re.escape(number_part)}\s',
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            start = m.start()
            if text[start] == '\n':
                start += 1

            # 次の条文番号（同レベル）で終了位置を決定
            # 例: "9.11" の次は "9.11A" or "9.12"
            base_num = number_part.split('.')[0]  # "9"
            next_pattern = rf'\n\s*(?:{re.escape(base_num)}\.\d+[A-Z]?\b|No\.?\s*{re.escape(base_num)}\.\d+[A-Z]?\b)'
            rest = text[m.end():]
            next_m = re.search(next_pattern, rest)
            if next_m:
                end = m.end() + next_m.start()
            else:
                end = min(m.end() + 1000, len(text))

            extracted = text[start:end].strip()
            if len(extracted) > 20:
                return extracted

    return None


def find_article_text(chunks: list, article_number: str) -> Optional[str]:
    """
    チャンクリストから条文番号に一致するテキストを検索する。
    1. article_noが完全一致するチャンク（Vol.1優先）
    2. チャンク内テキストから条文定義を切り出し（Vol.1優先）
    3. 部分一致フォールバック
    """
    article_number = article_number.strip()

    # 数字だけの場合は "No. X.XX" 形式に正規化
    if re.match(r"^\d+\.\d+", article_number):
        article_number = f"No. {article_number}"

    # No. 付きの番号を正規化（"No.9.21" → "No. 9.21" のようにスペースを統一）
    normalized = re.sub(r"No\.?\s*", "No. ", article_number)
    number_part = re.sub(r"No\.?\s*", "", normalized).strip()  # "9.11"

    vol_priority = {"Vol.1": 0, "Vol.2": 1, "Vol.3": 2, "Vol.4": 3, "RoP": 4}

    # ステップ1: article_noが完全一致するチャンクから、条文定義テキストを検索
    # Vol.1のチャンク内テキストから条文定義部分を切り出す試みも行う
    exact_matches = []
    inline_matches = []

    for chunk in chunks:
        vol = chunk.get("vol", "")
        priority = vol_priority.get(vol, 9)
        chunk_no = chunk.get("article_no", "").strip()
        chunk_normalized = re.sub(r"No\.?\s*", "No. ", chunk_no) if chunk_no else ""
        text = chunk.get("text", "")

        # article_no完全一致
        if chunk_normalized == normalized:
            exact_matches.append((priority, text))

        # テキスト内に条文定義が含まれているか
        # Vol.1のみ対象（条文本体の定義テキスト）
        # article_noが空 or 別の番号のチャンク内に定義がある場合を検出
        if vol == "Vol.1" and number_part in text:
            # article_noが一致しないチャンクからの抽出を優先
            # （大きなチャンクに埋もれた条文定義を見つける）
            is_other_chunk = chunk_normalized != normalized
            extracted = _extract_article_from_chunk(text, number_part)
            if extracted:
                # 別チャンクからの抽出は優先度ボーナス（-1）
                adj_priority = priority - 1 if is_other_chunk else priority
                inline_matches.append((adj_priority, extracted))

    # Vol.1のインライン抽出を最優先（条文定義そのもの）
    if inline_matches:
        inline_matches.sort(key=lambda x: x[0])
        return inline_matches[0][1]

    # article_no完全一致（Vol.1優先）
    if exact_matches:
        exact_matches.sort(key=lambda x: x[0])
        return exact_matches[0][1]

    # フォールバック: 部分一致（番号がテキスト冒頭に含まれるチャンク）
    partial_matches = []
    for chunk in chunks:
        text = chunk.get("text", "")
        if number_part and number_part in text[:100]:
            vol = chunk.get("vol", "")
            priority = vol_priority.get(vol, 9)
            partial_matches.append((priority, text))

    if partial_matches:
        partial_matches.sort(key=lambda x: x[0])
        return partial_matches[0][1]

    return None


def expand_node(article_number: str, article_text: str) -> dict:
    """
    Claude Sonnetに関連条文を問い合わせ、子ノード情報を返す。

    Returns:
        {
            "summary": "条文の要約（日本語）",
            "related": [
                {"number": "No.9.21", "reason": "理由", "node_id": "abc12345"}
            ]
        }
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"summary": "APIキーが設定されていません", "related": []}

    client = anthropic.Anthropic(api_key=api_key)

    # テキストが長すぎる場合は切り詰め
    truncated_text = article_text[:2000] if len(article_text) > 2000 else article_text

    try:
        response = client.messages.create(
            model=TREE_MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"以下の条文について、関連条文を特定してください。\n\n{article_number}\n{truncated_text}",
                }
            ],
        )
        raw = response.content[0].text.strip()

        # JSONパース（コードブロックで囲まれている場合に対応）
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        data = json.loads(raw)

        # 各関連条文にノードIDを付与
        for item in data.get("related", []):
            item["node_id"] = generate_node_id()

        return data

    except (json.JSONDecodeError, anthropic.APIError, KeyError) as e:
        return {"summary": f"解析エラー: {e}", "related": []}


def build_tree_markdown(tree_data: dict, indent: int = 0) -> str:
    """
    ツリーデータをMarkdown形式にエクスポートする。

    Args:
        tree_data: ツリーノードデータ
        indent: 現在のインデント深さ

    Returns:
        Markdown文字列
    """
    prefix = "  " * indent
    lines = []

    number = tree_data.get("number", "")
    summary = tree_data.get("summary", "")
    reason = tree_data.get("reason", "")
    text_preview = tree_data.get("text_preview", "")

    # ルートノード
    if indent == 0:
        lines.append(f"# {number}")
        if summary:
            lines.append(f"\n> {summary}\n")
        if text_preview:
            lines.append(f"```\n{text_preview[:300]}\n```\n")
    else:
        bullet = "- " if indent == 1 else "  " * (indent - 1) + "- "
        lines.append(f"{bullet}**{number}**")
        if reason:
            lines.append(f"{prefix}  - 理由: {reason}")
        if summary:
            lines.append(f"{prefix}  - 要約: {summary}")

    # 子ノードを再帰的に処理
    for child in tree_data.get("children", []):
        lines.append(build_tree_markdown(child, indent + 1))

    return "\n".join(lines)
