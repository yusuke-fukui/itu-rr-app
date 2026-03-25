"""
FAISSインデックスを使った条文検索モジュール。
ハイブリッド検索（キーワード一致 + ベクトル類似度）に対応。
"""

import json
import re
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

# パス設定
ROOT_DIR = Path(__file__).resolve().parent.parent
INDEX_DIR = ROOT_DIR / "data" / "index"

# 埋め込みモデル（indexer.pyと同じモデルを使用）
MODEL_NAME = "all-MiniLM-L6-v2"

# 検索モード
MODE_HYBRID = "hybrid"          # キーワード一致 + 意味検索
MODE_KEYWORD = "keyword"        # キーワード一致のみ
MODE_SEMANTIC = "semantic"      # 意味検索のみ


class RRSearcher:
    """ITU-RR条文検索クラス。"""

    def __init__(self):
        self.model = None
        self.index = None
        self.chunks = None

    def is_index_ready(self) -> bool:
        """インデックスファイルが存在するか確認する。"""
        return (INDEX_DIR / "faiss.index").exists() and (INDEX_DIR / "chunks.json").exists()

    def load(self):
        """インデックスとモデルを読み込む。"""
        if not self.is_index_ready():
            raise FileNotFoundError(
                "インデックスが見つかりません。先にインデックスを作成してください:\n"
                "  python src/indexer.py"
            )

        # FAISSインデックス読み込み
        self.index = faiss.read_index(str(INDEX_DIR / "faiss.index"))

        # メタデータ読み込み
        with open(INDEX_DIR / "chunks.json", "r", encoding="utf-8") as f:
            self.chunks = json.load(f)

        # 埋め込みモデル読み込み
        self.model = SentenceTransformer(MODEL_NAME)

    def _keyword_search(self, query: str) -> dict:
        """
        キーワード検索: クエリ文字列全体をフレーズとしてテキストに含まれるチャンクを検索する。
        大文字小文字を区別しない。
        複数語の場合はフレーズ一致（"harmful interference" → フレーズ全体で検索）。

        Returns:
            {chunk_index: keyword_score} のdict
        """
        phrase = query.strip()
        if not phrase:
            return {}
        phrase_lower = phrase.lower()

        # 条文番号パターンの検出（"9.12" → "No. 9.12" との照合用）
        is_article_query = bool(re.match(r"^\d+\.\d+[A-Z]?$", phrase))
        if is_article_query:
            query_normalized = f"no. {phrase_lower}"
        else:
            query_normalized = None

        # 条文番号クエリの場合、ワード境界付き正規表現を用意
        # "9.4" → "9.4" にマッチするが "9.41" にはマッチしない
        if is_article_query:
            # "9.4" → \b9\.4\b（ただし "9.4A" はOK）
            article_boundary_pattern = re.compile(
                rf'\b{re.escape(phrase)}(?![0-9])', re.IGNORECASE
            )
        else:
            article_boundary_pattern = None

        results = {}
        for i, chunk in enumerate(self.chunks):
            text = chunk.get("text", "")
            text_lower = text.lower()
            article_no = chunk.get("article_no", "").lower()
            article_no_normalized = re.sub(r"no\.?\s*", "no. ", article_no) if article_no else ""
            section_path = chunk.get("section_path", "").lower()
            # 検索対象テキスト（本文 + 条文番号 + セクションパス）
            searchable = f"{text_lower} {article_no} {section_path}"

            # 条文番号クエリの場合: ワード境界で完全一致チェック
            # "9.4" で "9.41" を引っかけないようにする
            if article_boundary_pattern:
                if not article_boundary_pattern.search(searchable):
                    continue
            else:
                # フレーズ全体が含まれているかチェック
                if phrase_lower not in searchable:
                    continue

            # スコア計算: ベーススコア 1.0
            # 出現回数ボーナス（多く出現するほどスコアアップ）
            if article_boundary_pattern:
                total_count = len(article_boundary_pattern.findall(searchable))
            else:
                total_count = searchable.count(phrase_lower)
            freq_bonus = min(total_count * 0.05, 0.3)  # 最大+0.3

            # 完全一致ボーナス（条文番号やセクションに直接含まれる場合）
            exact_bonus = 0
            if phrase_lower in article_no:
                exact_bonus = max(exact_bonus, 0.2)
            if phrase_lower in section_path:
                exact_bonus = max(exact_bonus, 0.1)

            # 条文番号クエリの場合: article_noが完全一致なら大ボーナス
            # "9.12" で検索 → article_no="No. 9.12" のチャンクを最優先
            article_exact_bonus = 0
            if query_normalized and article_no_normalized == query_normalized:
                article_exact_bonus = 0.5

            score = 1.0 + freq_bonus + exact_bonus + article_exact_bonus
            results[i] = min(score, 2.0)  # 上限2.0

        return results

    def _semantic_search(self, query: str, search_k: int = 2000) -> dict:
        """
        意味検索（ベクトル検索）: コサイン類似度で検索する。

        Returns:
            {chunk_index: semantic_score} のdict
        """
        # クエリをベクトル化（正規化済み）
        query_vector = self.model.encode(
            [query],
            normalize_embeddings=True,
        ).astype(np.float32)

        scores, indices = self.index.search(query_vector, search_k)

        results = {}
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self.chunks):
                continue
            if score > 0:
                results[int(idx)] = float(score)

        return results

    def _format_result(self, idx: int, score: float) -> dict:
        """チャンクインデックスとスコアから結果dictを作成する。"""
        chunk = self.chunks[idx]
        return {
            "article_no": chunk.get("article_no", ""),
            "vol": chunk.get("vol", ""),
            "pdf_page": chunk.get("pdf_page", chunk.get("page", 0)),
            "printed_page": chunk.get("printed_page", ""),
            "section_path": chunk.get("section_path", ""),
            "text": chunk.get("text", ""),
            "score": score,
            "match_type": "",  # 後で設定
        }

    @staticmethod
    def _matches_article(result: dict, article_num: str) -> bool:
        """
        結果がArticle番号に属するか判定する。
        section_path（ARTICLE X）またはarticle_no（No. X.YY）から判定。
        """
        section_path = result.get("section_path", "")
        article_no = result.get("article_no", "")

        # section_pathにARTICLE Xが含まれるか
        if f"ARTICLE {article_num}" in section_path:
            # 「ARTICLE 5」で検索時に「ARTICLE 54」等に誤マッチしないようチェック
            pattern = rf'ARTICLE\s+{re.escape(article_num)}(?:\s|$|[^0-9A-Z])'
            if re.search(pattern, section_path):
                return True

        # article_noからArticle番号を抽出（"No. 9.12" → Article 9）
        if article_no:
            m = re.match(r'No\.?\s*(\d+[A-Z]?)\.', article_no)
            if m and m.group(1) == article_num:
                return True

        return False

    def _apply_filters(self, results: list, vol_filter: str = "All",
                       sub_filter: str = "All") -> list:
        """
        検索結果にVolume/サブフィルタを適用する。

        Args:
            results: 検索結果リスト
            vol_filter: "All", "Vol.1", "Vol.2", "Vol.3", "Vol.4", "RoP"
            sub_filter: Vol.1の場合は "All" or "Article X"
                        Vol.2の場合は "All" or "ANNEX X"
        """
        if vol_filter == "All":
            return results

        # Volumeフィルタ
        filtered = [r for r in results if r.get("vol") == vol_filter]

        # サブフィルタ
        if sub_filter and sub_filter != "All":
            if vol_filter == "Vol.1" and sub_filter.startswith("Article "):
                article_num = sub_filter.replace("Article ", "")
                filtered = [
                    r for r in filtered
                    if self._matches_article(r, article_num)
                ]
            elif vol_filter == "Vol.2" and sub_filter.startswith("ANNEX "):
                filtered = [
                    r for r in filtered
                    if r.get("section_path", "").startswith(sub_filter)
                ]

        return filtered

    def search(self, query: str, top_k: int = 5, threshold: float = 0.30,
               mode: str = MODE_HYBRID, vol_filter: str = "All",
               sub_filter: str = "All") -> dict:
        """
        クエリに最も関連する条文を検索する。

        Args:
            query: 検索クエリ
            top_k: カード表示する件数
            threshold: 閾値（ハイブリッド/意味検索で使用）
            mode: 検索モード ("hybrid", "keyword", "semantic")
            vol_filter: Volumeフィルタ ("All", "Vol.1", "Vol.2", "Vol.3", "Vol.4", "RoP")
            sub_filter: サブフィルタ ("All", "Article X", "ANNEX X")

        Returns:
            {"results": [...], "all_hits": [...], "total_hits": int}
        """
        if self.index is None or self.chunks is None or self.model is None:
            self.load()

        if mode == MODE_KEYWORD:
            data = self._search_keyword_only(query, top_k)
        elif mode == MODE_SEMANTIC:
            data = self._search_semantic_only(query, top_k, threshold)
        else:
            data = self._search_hybrid(query, top_k, threshold)

        # フィルタ適用
        if vol_filter != "All":
            all_hits = self._apply_filters(data["all_hits"], vol_filter, sub_filter)
            data["all_hits"] = all_hits
            data["results"] = all_hits[:top_k]
            data["total_hits"] = len(all_hits)

        return data

    def _search_keyword_only(self, query: str, top_k: int) -> dict:
        """キーワード検索のみ。"""
        keyword_scores = self._keyword_search(query)

        if not keyword_scores:
            return {"results": [], "all_hits": [], "total_hits": 0}

        # スコア降順でソート
        sorted_indices = sorted(keyword_scores.keys(),
                                key=lambda i: keyword_scores[i], reverse=True)

        all_hits = []
        for idx in sorted_indices:
            result = self._format_result(idx, keyword_scores[idx])
            result["match_type"] = "keyword"
            # キーワードスコアを0〜1に正規化して表示
            result["score"] = min(keyword_scores[idx] / 2.0, 1.0)
            all_hits.append(result)

        return {
            "results": all_hits[:top_k],
            "all_hits": all_hits,
            "total_hits": len(all_hits),
        }

    def _search_semantic_only(self, query: str, top_k: int, threshold: float) -> dict:
        """意味検索のみ（従来の方式）。"""
        semantic_scores = self._semantic_search(query)

        all_hits = []
        for idx in sorted(semantic_scores.keys(),
                          key=lambda i: semantic_scores[i], reverse=True):
            score = semantic_scores[idx]
            if score < threshold:
                continue
            result = self._format_result(idx, score)
            result["match_type"] = "semantic"
            all_hits.append(result)

        return {
            "results": all_hits[:top_k],
            "all_hits": all_hits,
            "total_hits": len(all_hits),
        }

    def _search_hybrid(self, query: str, top_k: int, threshold: float) -> dict:
        """
        ハイブリッド検索: キーワード一致と意味検索を統合。
        キーワード一致を優先し、意味検索で補完する。
        """
        keyword_scores = self._keyword_search(query)
        semantic_scores = self._semantic_search(query)

        # 全チャンクインデックスを統合
        all_indices = set(keyword_scores.keys()) | set(semantic_scores.keys())

        combined = {}
        for idx in all_indices:
            kw_score = keyword_scores.get(idx, 0)
            sem_score = semantic_scores.get(idx, 0)

            # ハイブリッドスコア計算:
            #   - キーワード一致があればそれを大きく重み付け
            #   - 意味検索スコアは補助
            if kw_score > 0 and sem_score > 0:
                # 両方マッチ → 最強（キーワード一致 × 0.6 + 意味 × 0.4 + ボーナス0.1）
                hybrid = (kw_score / 2.0) * 0.6 + sem_score * 0.4 + 0.1
                match_type = "hybrid"
            elif kw_score > 0:
                # キーワードのみ
                hybrid = (kw_score / 2.0) * 0.8
                match_type = "keyword"
            else:
                # 意味検索のみ
                hybrid = sem_score * 0.7
                match_type = "semantic"

            combined[idx] = (min(hybrid, 1.0), match_type)

        # スコア降順でソート
        sorted_indices = sorted(combined.keys(),
                                key=lambda i: combined[i][0], reverse=True)

        # 閾値フィルタ（ハイブリッドでは低めの閾値を適用）
        effective_threshold = threshold * 0.5  # ハイブリッドでは閾値を緩く
        all_hits = []
        for idx in sorted_indices:
            score, match_type = combined[idx]
            if score < effective_threshold:
                continue
            result = self._format_result(idx, score)
            result["match_type"] = match_type
            all_hits.append(result)

        return {
            "results": all_hits[:top_k],
            "all_hits": all_hits,
            "total_hits": len(all_hits),
        }
