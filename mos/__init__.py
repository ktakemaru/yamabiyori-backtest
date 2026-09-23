"""移植可能な予報補正 (MOS) モジュール。標準ライブラリのみ、I/O なし。

移植先へは mos/ ディレクトリを丸ごとコピーし、表 (mos_tables/*.json) は別に置いて json.load → parse_table_set で読む。
設計: docs/mos-module-design.md (yamabiyori-backtest)。
"""
from .calibrate import ON_MISMATCH_CHOICES, Result, Selection, bin_index, calibrate, effective_value, probability, select_table
from .errors import ContextMismatchError, MosError, SchemaError, TableNotFoundError
from .tables import MOS_VERSION, SCHEMA_NAME, SUPPORTED_SCHEMA_VERSIONS, Table, TableSet, Transform, content_sha256, parse_table_set

__version__ = MOS_VERSION
__all__ = [
    "MOS_VERSION", "SCHEMA_NAME", "SUPPORTED_SCHEMA_VERSIONS", "ON_MISMATCH_CHOICES",
    "parse_table_set", "content_sha256", "select_table", "probability", "effective_value", "calibrate", "bin_index",
    "Table", "TableSet", "Transform", "Selection", "Result",
    "MosError", "SchemaError", "TableNotFoundError", "ContextMismatchError",
]
