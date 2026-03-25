"""
ローカルストレージラッパー。
将来的なクラウド移行（Firebase等）を考慮し、データアクセスを抽象化する。
現時点ではローカルJSONファイルで永続化。
"""

import json
from pathlib import Path
from typing import Any, Optional

# デフォルトの保存先
DEFAULT_STORAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "user_data"


class LocalStorage:
    """ローカルJSONファイルベースのストレージ。"""

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or DEFAULT_STORAGE_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, collection: str) -> Path:
        return self.storage_dir / f"{collection}.json"

    def load(self, collection: str) -> Any:
        """コレクション（JSONファイル）を読み込む。存在しなければNoneを返す。"""
        path = self._file_path(collection)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, collection: str, data: Any) -> None:
        """コレクション（JSONファイル）に保存する。"""
        path = self._file_path(collection)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def delete(self, collection: str) -> None:
        """コレクション（JSONファイル）を削除する。"""
        path = self._file_path(collection)
        if path.exists():
            path.unlink()

    def exists(self, collection: str) -> bool:
        """コレクションが存在するか確認する。"""
        return self._file_path(collection).exists()


# シングルトンインスタンス
_storage = LocalStorage()


def get_storage() -> LocalStorage:
    """ストレージインスタンスを返す。"""
    return _storage
