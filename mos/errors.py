"""mos の例外。呼び出し側は MosError だけ捕まえれば全部拾える。"""


class MosError(Exception):
    """mos が出す例外の基底。"""


class SchemaError(MosError):
    """表の JSON が読めない: スキーマ名・版・content_sha256・必須キー・値域のいずれかが合わない。"""


class TableNotFoundError(MosError):
    """(説明変数, 目的変数) に合う表が無い。on_mismatch では回避できない。"""


class ContextMismatchError(MosError):
    """表の適用範囲 (時刻・月) の外で引こうとした (on_mismatch="error" のとき)。"""
