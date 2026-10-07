from __future__ import annotations

import argparse
import codecs
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
import difflib
import hashlib
from itertools import combinations
import math
import os
import re as _re
from typing import Any

import chardet
import pandas as pd

# ============================================================
# PATH SAFETY
# ============================================================


def safe_path(path: str, must_exist: bool = True) -> str:
    """Resolve to an absolute canonical path and reject anything unsafe."""
    real = os.path.realpath(os.path.abspath(path))
    root = os.path.realpath(os.getcwd())

    if os.path.commonpath([real, root]) != root:
        raise ValueError(f"Path outside working directory: {path}")

    if must_exist and not os.path.isfile(real):
        raise FileNotFoundError(f"Not a file: {path}")

    return real


# ============================================================
# CONFIGURATION
# ============================================================


@dataclass
class ReconciliationConfig:
    legacy_file: str
    new_file: str

    delimiter: str = ""
    precision_digits: int = 2

    encoding: str = "utf-8"
    legacy_encoding: str = ""
    new_encoding: str = ""
    legacy_encoding_label: str = ""
    new_encoding_label: str = ""

    legacy_delimiter: str = ""
    new_delimiter: str = ""

    quotechar: str = '"'
    doublequote: bool = True

    legacy_bad_lines: list = None
    new_bad_lines: list = None

    # Candidate matching
    min_similarity: float = 0.75
    ambiguity_margin: float = 0.05
    ambiguity_max_column_diff: float = 1.0

    # Sparsity threshold for candidate evaluation (ignore columns >= 70% null/empty)
    sparsity_threshold: float = 0.70

    max_block_columns: int = 3
    min_uniqueness_ratio: float = 0.01
    max_block_frequency: float = 0.10
    max_candidates_per_row: int = 100
    small_file_row_threshold: int = 50
    min_cross_file_overlap: float = 0.5

    # Reporting
    sample_limit: int = 5
    output_directory: str = "reconciliation_output"
    null_token: str = "<NULL>"
    max_grouping_cardinality: int = 20
    user_keys: list[str] = None  # Explicit user-provided join key columns


# ============================================================
# ENCODING DETECTION
# ============================================================

_DETECT_SAMPLE_BYTES = 10_000_000


def detect_encoding(filepath: str) -> tuple[str, float]:
    with open(safe_path(filepath), "rb") as f:
        raw = f.read(_DETECT_SAMPLE_BYTES)

    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", 1.0
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", 1.0

    result = chardet.detect(raw)
    encoding = result.get("encoding") or "utf-8"
    confidence = result.get("confidence") or 0.0

    return encoding, confidence


def _decodes_cleanly(filepath: str, codec: str) -> bool:
    try:
        with open(safe_path(filepath), "r", encoding=codec, newline="") as f:
            while f.read(1 << 20):
                pass
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def resolve_encoding(
    explicit: str, filepath: str, label: str
) -> tuple[str, str, str]:
    if explicit:
        print(f"  {label}: {explicit} (explicit)")
        return explicit, "explicit", explicit

    # 1. First test if the FULL file decodes cleanly as UTF-8
    if _decodes_cleanly(filepath, "utf-8"):
        print(f"  {label}: utf-8 (verified full file decode)")
        return "utf-8", "auto-resolved", "utf-8"

    # 2. If UTF-8 fails (e.g. legacy CP1252 single-byte £ character 0xA3), detect fallback
    detected, confidence = detect_encoding(filepath)
    codec = "utf-8" if detected.lower() == "ascii" else detected

    if _decodes_cleanly(filepath, codec):
        print(
            f"  {label}: {codec} (detected fallback, confidence={confidence:.0%})"
        )
        return codec, "detected", detected

    # 3. Final fallback for legacy ANSI / Windows Western encodings
    for fallback in ("cp1252", "latin-1"):
        if _decodes_cleanly(filepath, fallback):
            print(f"  {label}: {fallback} (fallback)")
            return fallback, "fallback", fallback

    return "utf-8", "default", "utf-8"


# ============================================================
# EOL / NEWLINE DETECTION
# ============================================================


def detect_eol(filepath: str, encoding: str, delimiter=",") -> dict:
    with open(safe_path(filepath), "rb") as f:
        raw = f.read()

    crlf_count = raw.count(b"\r\n")
    cr_count = sum(
        1
        for i, b in enumerate(raw)
        if b == ord(b"\r")
        and (i + 1 >= len(raw) or raw[i + 1] != ord(b"\n"))
    )
    lf_count = sum(
        1
        for i, b in enumerate(raw)
        if b == ord(b"\n") and (i == 0 or raw[i - 1] != ord(b"\r"))
    )

    eol_counts = {"CRLF": crlf_count, "LF": lf_count, "CR": cr_count}
    active = [k for k, v in eol_counts.items() if v > 0]

    if len(active) == 0:
        file_eol = "NONE"
    elif len(active) == 1:
        file_eol = active[0]
    else:
        file_eol = "MIXED"

    embedded = []
    try:
        df = pd.read_csv(
            filepath,
            encoding=encoding,
            delimiter=delimiter,
            dtype=str,
            keep_default_na=False,
        )

        for col in df.columns:
            for row_idx, val in df[col].items():
                if isinstance(val, str) and ("\n" in val or "\r" in val):
                    embedded.append(
                        {
                            "row": row_idx + 2,
                            "col": col,
                            "value": repr(val),
                        }
                    )
    except Exception:
        pass

    return {
        "file_eol": file_eol,
        "eol_counts": eol_counts,
        "embedded_newline_rows": embedded,
    }


# ============================================================
# DELIMITER DETECTION
# ============================================================


def detect_delimiter(filepath: str, encoding: str) -> str:
    with open(safe_path(filepath), "r", encoding=encoding, newline="") as f:
        sample = f.read(65_536)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",|;\t").delimiter
    except csv.Error:
        header = sample.splitlines()[0] if sample else ""
        counts = {d: header.count(d) for d in ",|;\t"}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


# ============================================================
# BAD LINE DETECTION
# ============================================================


def _prescan_short_lines(
    filepath: str, encoding: str, delimiter: str, quotechar: str
) -> list:
    with open(safe_path(filepath), "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=delimiter, quotechar=quotechar)
        rows = list(reader)

    if not rows:
        return []

    expected = len(rows[0])
    short = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < expected:
            short.append(
                {
                    "row": i,
                    "field_count": len(row),
                    "raw_line": delimiter.join(row),
                }
            )
    return short


class _BadLineCollector:

    def __init__(self):
        self.skipped = []

    def __call__(self, bad_line):
        self.skipped.append(bad_line)
        return None


# ============================================================
# CSV LOADING
# ============================================================


def load_csv(config: ReconciliationConfig):
    print("\nResolving file encodings:")

    legacy_enc, _, legacy_label = resolve_encoding(
        config.legacy_encoding,
        config.legacy_file,
        "legacy",
    )

    new_enc, _, new_label = resolve_encoding(
        config.new_encoding,
        config.new_file,
        "new   ",
    )

    config.legacy_encoding = legacy_enc
    config.new_encoding = new_enc
    config.legacy_encoding_label = legacy_label
    config.new_encoding_label = new_label

    if config.delimiter:
        legacy_delim = new_delim = config.delimiter
        print(f"  delimiter: {config.delimiter!r} (explicit)")
    else:
        legacy_delim = detect_delimiter(config.legacy_file, legacy_enc)
        new_delim = detect_delimiter(config.new_file, new_enc)
        print(
            f"  delimiter: legacy={legacy_delim!r}, new={new_delim!r} (auto-detected)"
        )

    config.legacy_delimiter = legacy_delim
    config.new_delimiter = new_delim

    legacy_short = _prescan_short_lines(
        config.legacy_file, legacy_enc, legacy_delim, config.quotechar
    )
    new_short = _prescan_short_lines(
        config.new_file, new_enc, new_delim, config.quotechar
    )

    legacy_bad = _BadLineCollector()
    legacy_df = pd.read_csv(
        config.legacy_file,
        delimiter=legacy_delim,
        encoding=legacy_enc,
        dtype=str,
        keep_default_na=False,
        quotechar=config.quotechar,
        doublequote=config.doublequote,
        quoting=0,
        engine="python",
        on_bad_lines=legacy_bad,
    )

    new_bad = _BadLineCollector()
    new_df = pd.read_csv(
        config.new_file,
        delimiter=new_delim,
        encoding=new_enc,
        dtype=str,
        keep_default_na=False,
        quotechar=config.quotechar,
        doublequote=config.doublequote,
        quoting=0,
        engine="python",
        on_bad_lines=new_bad,
    )

    legacy_short_rows = {b["row"] - 2 for b in legacy_short}
    new_short_rows = {b["row"] - 2 for b in new_short}
    if legacy_short_rows:
        legacy_df = legacy_df.drop(
            index=[i for i in legacy_short_rows if i in legacy_df.index]
        )
    if new_short_rows:
        new_df = new_df.drop(
            index=[i for i in new_short_rows if i in new_df.index]
        )

    config.legacy_bad_lines = legacy_bad.skipped + legacy_short
    config.new_bad_lines = new_bad.skipped + new_short

    if config.legacy_bad_lines:
        print(
            f"  legacy: {len(config.legacy_bad_lines)} row(s) skipped (delimiter count mismatch)"
        )
    if config.new_bad_lines:
        print(
            f"  new   : {len(config.new_bad_lines)} row(s) skipped (delimiter count mismatch)"
        )

    return legacy_df, new_df


# ============================================================
# DUPLICATE ROW AUDIT (FOR REPORTING ONLY)
# ============================================================


def audit_duplicate_rows(
    df: pd.DataFrame,
    null_token: str,
    label: str,
) -> list[dict]:
    seen_fingerprints = {}
    duplicate_records = []

    for idx, row in df.iterrows():
        fp = row_fingerprint(row, null_token)
        if fp in seen_fingerprints:
            duplicate_records.append(
                {
                    "source": label,
                    "file_row": idx + 2,
                    "first_seen_file_row": seen_fingerprints[fp] + 2,
                    "values": row.tolist(),
                }
            )
        else:
            seen_fingerprints[fp] = idx

    return duplicate_records


# ============================================================
# RAW & MATCHING VALUES + NULL-AWARENESS
# ============================================================

_NULL_STRINGS = {
    "",
    "null",
    "<null>",
    "none",
    "<none>",
    "nan",
    "n/a",
    "#na",
}


def build_user_key_index(
    df: pd.DataFrame,
    key_positions: list[int],
    null_token: str,
) -> dict[tuple[str, ...], list[int]]:
    """Build a lookup map hashed strictly on the normalized user-defined key columns."""
    key_map = defaultdict(list)
    for idx, row in df.iterrows():
        key_tuple = tuple(
            matching_value(row.iloc[pos], null_token) for pos in key_positions
        )
        # Only index if at least one key column is non-null
        if any(k != null_token for k in key_tuple):
            key_map[key_tuple].append(idx)
    return key_map


def is_null_like(value: Any, null_token: str) -> bool:
    """Return True if value is None, NaN, empty, or a string literal like 'NULL' or '<NULL>'."""
    if value is None or pd.isna(value):
        return True
    val_str = str(value).strip().lower()
    return val_str in _NULL_STRINGS or val_str == null_token.lower()


def raw_value(value: Any, null_token: str) -> str:
    if value is None or pd.isna(value):
        return null_token
    return str(value)


def _norm_trailing(s: str) -> str:
    s = s.replace(",", "")
    m = _re.match(r"^(.*?\d)\.(\d*?)0*(\D*)$", s)
    if not m:
        return s
    head, frac, tail = m.groups()
    return f"{head}.{frac}{tail}" if frac else f"{head}{tail}"


def matching_value(value: Any, null_token: str) -> str:
    if is_null_like(value, null_token):
        return null_token
    return _norm_trailing(str(value).strip().lower())


def build_matching_dataframe(df: pd.DataFrame, null_token: str):
    result = pd.DataFrame(index=df.index)
    for column in df.columns:
        result[column] = df[column].map(
            lambda x: matching_value(x, null_token)
        )
    return result


def row_fingerprint(row: pd.Series, null_token: str):
    values = [raw_value(value, null_token) for value in row.tolist()]
    payload = "\x1f".join(values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ============================================================
# SPARSE COLUMN IDENTIFICATION
# ============================================================


def identify_sparse_columns(
    legacy_df: pd.DataFrame,
    new_df: pd.DataFrame,
    null_token: str,
    threshold: float = 0.70,
) -> set[int]:

    sparse_positions = set()
    total_cols = min(legacy_df.shape[1], new_df.shape[1])

    for pos in range(total_cols):
        l_col = legacy_df.iloc[:, pos]
        n_col = new_df.iloc[:, pos]

        l_empty = sum(1 for v in l_col if is_null_like(v, null_token))
        n_empty = sum(1 for v in n_col if is_null_like(v, null_token))

        l_rate = l_empty / max(len(l_col), 1)
        n_rate = n_empty / max(len(n_col), 1)

        if l_rate >= threshold or n_rate >= threshold:
            sparse_positions.add(pos)

    return sparse_positions


# ============================================================
# STRUCTURAL VALIDATION
# ============================================================


def validate_structure(legacy_df: pd.DataFrame, new_df: pd.DataFrame):
    legacy_columns = list(legacy_df.columns)
    new_columns = list(new_df.columns)

    result = {
        "legacy_column_count": len(legacy_columns),
        "new_column_count": len(new_columns),
        "column_count_match": len(legacy_columns) == len(new_columns),
        "column_differences": [],
    }

    max_columns = max(len(legacy_columns), len(new_columns))

    for position in range(max_columns):
        legacy_name = (
            legacy_columns[position] if position < len(legacy_columns) else None
        )
        new_name = (
            new_columns[position] if position < len(new_columns) else None
        )

        if legacy_name != new_name:
            result["column_differences"].append(
                {
                    "position": position + 1,
                    "legacy_column": legacy_name,
                    "new_column": new_name,
                }
            )

    return result


# ============================================================
# EXACT RAW MATCHING
# ============================================================


def find_exact_raw_matches(
    legacy_df: pd.DataFrame,
    new_df: pd.DataFrame,
    config: ReconciliationConfig,
):
    new_hash_map = defaultdict(list)

    for new_idx, row in new_df.iterrows():
        fingerprint = row_fingerprint(row, config.null_token)
        new_hash_map[fingerprint].append(new_idx)

    matched_pairs = []
    used_new = set()
    unmatched_legacy = []

    for legacy_idx, row in legacy_df.iterrows():
        fingerprint = row_fingerprint(row, config.null_token)
        candidates = new_hash_map.get(fingerprint, [])

        selected = None
        for candidate in candidates:
            if candidate not in used_new:
                selected = candidate
                break

        if selected is not None:
            matched_pairs.append(
                {
                    "legacy_index": legacy_idx,
                    "new_index": selected,
                    "match_type": "EXACT_RAW",
                    "similarity": 1.0,
                }
            )
            used_new.add(selected)
        else:
            unmatched_legacy.append(legacy_idx)

    unmatched_new = [idx for idx in new_df.index if idx not in used_new]

    return matched_pairs, unmatched_legacy, unmatched_new


# ============================================================
# SELECT BLOCKING COLUMNS & INDEXES
# ============================================================


def select_blocking_columns(
    normalized_df: pd.DataFrame,
    config: ReconciliationConfig,
    other_normalized_df: pd.DataFrame = None,
):
    row_count = len(normalized_df)
    if row_count == 0:
        return []

    is_small = row_count <= config.small_file_row_threshold
    frequency_limit = 1.0 if is_small else config.max_block_frequency

    scored = []

    for position, column in enumerate(normalized_df.columns):
        value_counts = normalized_df.iloc[:, position].value_counts(
            dropna=False
        )
        uniqueness_ratio = len(value_counts) / row_count
        largest_frequency = (
            value_counts.iloc[0] / row_count if len(value_counts) else 1.0
        )
        overlap = 1.0

        if (
            other_normalized_df is not None
            and position < other_normalized_df.shape[1]
        ):
            new_vals = set(normalized_df.iloc[:, position])
            other_vals = set(other_normalized_df.iloc[:, position])
            overlap = len(new_vals & other_vals) / max(len(other_vals), 1)

        scored.append(
            {
                "position": position,
                "column": column,
                "uniqueness_ratio": uniqueness_ratio,
                "largest_frequency": largest_frequency,
                "overlap": overlap,
            }
        )

    strict = [
        c
        for c in scored
        if c["uniqueness_ratio"] >= config.min_uniqueness_ratio
        and c["largest_frequency"] <= frequency_limit
        and c["overlap"] >= config.min_cross_file_overlap
    ]

    if strict:
        chosen = strict
    else:
        chosen = sorted(
            [c for c in scored if c["overlap"] > 0],
            key=lambda c: (c["overlap"], c["uniqueness_ratio"]),
            reverse=True,
        )[: config.max_block_columns]

    chosen.sort(
        key=lambda c: (c["overlap"], c["uniqueness_ratio"]),
        reverse=True,
    )

    return chosen


def build_block_indexes(
    normalized_new_df: pd.DataFrame,
    blocking_columns,
    max_size: int = 3,
):
    indexes = {}
    column_positions = [item["position"] for item in blocking_columns]
    max_size = min(max_size, len(column_positions))

    for size in range(1, max_size + 1):
        for combo in combinations(column_positions, size):
            lookup = defaultdict(list)
            for idx, row in normalized_new_df.iterrows():
                key = tuple(row.iloc[position] for position in combo)
                lookup[key].append(idx)
            indexes[combo] = lookup

    return indexes


# ============================================================
# GENERATE CANDIDATES & ROW SIMILARITY
# ============================================================


def generate_candidates(
    legacy_row: pd.Series,
    normalized_legacy_df: pd.DataFrame,
    normalized_new_df: pd.DataFrame,
    block_indexes,
    blocking_columns,
    used_new_indexes,
    config: ReconciliationConfig,
    key_index_new: dict[tuple[str, ...], list[int]] | None = None,
    user_key_positions: list[int] | None = None,
):
    # Check strict user keys first if configured
    if key_index_new is not None and user_key_positions:
        key_tuple = tuple(
            matching_value(legacy_row.iloc[pos], config.null_token)
            for pos in user_key_positions
        )
        if any(k != config.null_token for k in key_tuple):
            matches = key_index_new.get(key_tuple, [])
            candidates = [m for m in matches if m not in used_new_indexes]
            return candidates

    candidate_scores = Counter()
    legacy_idx = legacy_row.name
    normalized_legacy_row = normalized_legacy_df.loc[legacy_idx]
    column_positions = [item["position"] for item in blocking_columns]
    max_size = min(config.max_block_columns, len(column_positions))

    for size in range(max_size, 0, -1):
        for combo in combinations(column_positions, size):
            lookup = block_indexes.get(combo)
            if lookup is None:
                continue

            key = tuple(
                normalized_legacy_row.iloc[position] for position in combo
            )
            matches = lookup.get(key, [])

            for new_idx in matches:
                if new_idx not in used_new_indexes:
                    candidate_scores[new_idx] += size

        if candidate_scores and size >= 2:
            break

    if not candidate_scores:
        remaining = [
            idx
            for idx in normalized_new_df.index
            if idx not in used_new_indexes
        ]
        if len(remaining) <= config.max_candidates_per_row:
            for idx in remaining:
                candidate_scores[idx] = 0

    candidates = list(candidate_scores.keys())

    if len(candidates) > config.max_candidates_per_row:
        candidates.sort(key=lambda idx: candidate_scores[idx], reverse=True)
        candidates = candidates[: config.max_candidates_per_row]

    return candidates


def row_similarity(
    legacy_row: pd.Series,
    new_row: pd.Series,
    config: ReconciliationConfig,
    sparse_positions: set[int] = None,
):
    total_columns = len(legacy_row)
    if total_columns == 0:
        return 0.0

    sparse_positions = sparse_positions or set()
    matches = 0
    evaluated_cols = 0

    for pos, (legacy_value, new_value) in enumerate(
        zip(legacy_row.tolist(), new_row.tolist())
    ):
        if pos in sparse_positions:
            continue

        evaluated_cols += 1
        if matching_value(
            legacy_value, config.null_token
        ) == matching_value(new_value, config.null_token):
            matches += 1

    if evaluated_cols == 0:
        return (
            sum(
                1
                for a, b in zip(legacy_row.tolist(), new_row.tolist())
                if matching_value(a, config.null_token)
                == matching_value(b, config.null_token)
            )
            / total_columns
        )

    return matches / evaluated_cols


# ============================================================
# BEST CANDIDATE (SPARSITY MASK + NULL-SAFE STRICT ID CHECK)
# ============================================================


def find_best_candidate(
    legacy_row: pd.Series,
    new_df: pd.DataFrame,
    candidate_indexes,
    config: ReconciliationConfig,
    id_positions=None,
    sparse_positions: set[int] = None,
):
    if not candidate_indexes:
        return (None, 0.0, 0.0, "UNRESOLVED")

    scores = []
    for new_idx in candidate_indexes:
        new_row = new_df.loc[new_idx]
        scores.append(
            (
                new_idx,
                row_similarity(legacy_row, new_row, config, sparse_positions),
            )
        )

    scores.sort(key=lambda x: x[1], reverse=True)

    best_idx, best_score = scores[0]
    second_best_score = scores[1][1] if len(scores) > 1 else 0.0

    total_cols = len(legacy_row)
    if total_cols == 0:
        return (None, 0.0, 0.0, "UNRESOLVED")

    score_gap = best_score - second_best_score

    # 1. COLUMN-AWARE DYNAMIC FLOOR DROP
    clear_winner_gap = 1.0 / total_cols
    is_clear_winner = (len(scores) == 1) or (score_gap >= clear_winner_gap)

    max_floor_col_drop = 7.5
    effective_floor_drop = (
        (max_floor_col_drop / total_cols) if is_clear_winner else 0.0
    )
    effective_min_similarity = max(
        0.50, config.min_similarity - effective_floor_drop
    )

    if best_score < effective_min_similarity:
        return (None, best_score, second_best_score, "UNRESOLVED")

    # 2. EVALUATE AMBIGUITY
    confidence_multiplier = max(0.5, 1.5 - best_score)
    base_col_margin = config.ambiguity_max_column_diff / total_cols
    effective_margin = base_col_margin * confidence_multiplier

    if len(scores) > 1 and score_gap < effective_margin:
        if id_positions:
            best_row = new_df.loc[best_idx]
            second_row = new_df.loc[scores[1][0]]

            best_id_match = all(
                matching_value(legacy_row.iloc[p], config.null_token)
                == matching_value(best_row.iloc[p], config.null_token)
                for p in id_positions
            )
            second_id_match = all(
                matching_value(legacy_row.iloc[p], config.null_token)
                == matching_value(second_row.iloc[p], config.null_token)
                for p in id_positions
            )

            if best_id_match and not second_id_match:
                return (best_idx, best_score, second_best_score, "MATCHED")

        return (None, best_score, second_best_score, "AMBIGUOUS")

    # 3. NULL-SAFE STRICT ID MISMATCH SAFETY CHECK
    if id_positions:
        new_row = new_df.loc[best_idx]
        id_mismatch = False
        for p in id_positions:
            leg_v = matching_value(legacy_row.iloc[p], config.null_token)
            new_v = matching_value(new_row.iloc[p], config.null_token)

            if leg_v != config.null_token and new_v != config.null_token:
                if leg_v != new_v:
                    id_mismatch = True
                    break

        if id_mismatch:
            return (None, best_score, second_best_score, "UNRESOLVED")

    return (best_idx, best_score, second_best_score, "MATCHED")


# ============================================================
# RAW COLUMN COMPARISON & CLASSIFICATION
# ============================================================


def compare_rows_raw(
    legacy_row: pd.Series,
    new_row: pd.Series,
    legacy_columns,
    new_columns,
    null_token: str,
):
    differences = []
    for position in range(len(legacy_columns)):
        legacy_value = raw_value(legacy_row.iloc[position], null_token)
        new_value = raw_value(new_row.iloc[position], null_token)

        if legacy_value != new_value:
            differences.append(
                {
                    "position": position + 1,
                    "legacy_column": legacy_columns[position],
                    "new_column": new_columns[position],
                    "legacy_value": legacy_value,
                    "new_value": new_value,
                }
            )

    return differences


_PRECISION_DIGITS = 2


def _classify_core(legacy_value: str, new_value: str) -> str:
    def split_frac(s):
        m = _re.match(r"^(.*?\d)\.(\d+)(\D*)$", s.strip())
        if not m:
            return None
        return m.group(1), m.group(2), m.group(3)

    if (", " in legacy_value) != ("," in new_value) and legacy_value.replace(
        ",", ""
    ) == new_value.replace(",", ""):
        return "THOUSANDS_SEPARATOR"

    _num_re = _re.compile(r"^[+-]?(\d+\.\d*|\.\d+)$")

    def _strip_lead_zero(s):
        return _re.sub(r"^([+-]?)0+(?=\.)", r"\1", s)

    def _strip_trail_zero(s):
        s = _re.sub(r"0+$", "", s) if "." in s else s
        return s[:-1] if s.endswith(".") else s

    def _has_lead_zero(s):
        return bool(_re.match(r"^[+-]?0+\.", s))

    if (
        _num_re.match(legacy_value)
        and _num_re.match(new_value)
        and _has_lead_zero(legacy_value) != _has_lead_zero(new_value)
        and _strip_trail_zero(_strip_lead_zero(legacy_value))
        == _strip_trail_zero(_strip_lead_zero(new_value))
    ):
        return "STARTING_ZERO"

    l = split_frac(legacy_value)
    n = split_frac(new_value)

    if l is None or n is None:
        return "EXACT_VALUES"

    l_head, l_frac, l_tail = l
    n_head, n_frac, n_tail = n

    if l_head != n_head or l_tail != n_tail:
        return "EXACT_VALUES"

    l_stripped = l_frac.rstrip("0")
    n_stripped = n_frac.rstrip("0")

    if l_stripped == n_stripped:
        return "TRAILING_ZEROS_ONLY"

    q = Decimal(1).scaleb(-_PRECISION_DIGITS)
    try:
        l_dec = Decimal(legacy_value.strip())
        n_dec = Decimal(new_value.strip())
        if l_dec.quantize(q, ROUND_DOWN) == n_dec.quantize(q, ROUND_DOWN):
            if l_dec != l_dec.quantize(
                q, ROUND_DOWN
            ) or n_dec != n_dec.quantize(q, ROUND_DOWN):
                return "PRECISION_MISMATCH"
    except Exception:
        pass

    return "EXACT_VALUES"


def classify_difference(legacy_value: str, new_value: str) -> str:
    l_core, n_core = legacy_value.strip(), new_value.strip()

    if l_core == n_core:
        return "WHITESPACE_ONLY"

    if legacy_value != l_core or new_value != n_core:
        return "WHITESPACE_AND_" + _classify_core(l_core, n_core)

    return _classify_core(legacy_value, new_value)


def difference_signature(differences):
    return tuple(
        (
            difference["position"],
            difference["legacy_column"],
            difference["new_column"],
        )
        for difference in differences
    )


# ============================================================
# MAIN RECONCILIATION
# ============================================================


def reconcile(
    legacy_df: pd.DataFrame,
    new_df: pd.DataFrame,
    config: ReconciliationConfig,
):
    legacy_columns = list(legacy_df.columns)
    new_columns = list(new_df.columns)

    id_positions = [
        i for i, col in enumerate(legacy_columns) if _ID_PATTERNS.search(col)
    ]

    # Resolve explicit key positions if provided
    user_key_positions = []
    if getattr(config, "user_keys", None):
        for key_col in config.user_keys:
            if key_col in legacy_columns:
                user_key_positions.append(legacy_columns.index(key_col))
            else:
                print(
                    f"  WARNING: Explicit key column '{key_col}' not found in file schema."
                )

    if user_key_positions:
        print(
            f"  candidate matching: STRICT KEY MATCHING on {config.user_keys}"
        )
        key_index_new = build_user_key_index(
            new_df, user_key_positions, config.null_token
        )
    else:
        key_index_new = None

    sparse_positions = identify_sparse_columns(
        legacy_df, new_df, config.null_token, config.sparsity_threshold
    )

    if sparse_positions:
        print(
            f"  candidate matching: masking {len(sparse_positions)} sparse column(s) (>=70% NULL/empty)"
        )

    normalized_legacy = build_matching_dataframe(legacy_df, config.null_token)
    normalized_new = build_matching_dataframe(new_df, config.null_token)

    matched_pairs, unmatched_legacy, unmatched_new = find_exact_raw_matches(
        legacy_df, new_df, config
    )

    used_new_indexes = {pair["new_index"] for pair in matched_pairs}

    blocking_columns = select_blocking_columns(
        normalized_new, config, normalized_legacy
    )

    block_indexes = build_block_indexes(
        normalized_new, blocking_columns, config.max_block_columns
    )

    detailed_results = []
    unresolved_records = []

    def _bad_line_text(b):
        if isinstance(b, dict):
            return b.get("raw_line", "")
        return " ".join(str(x) for x in b)

    skipped_new_blob = "\x1f".join(
        _bad_line_text(b)
        for b in (getattr(config, "new_bad_lines", None) or [])
    )

    column_mismatch_counts = Counter()
    difference_signature_counts = Counter()
    column_error_type_counts = Counter()
    samples_by_error_type = {}

    error_type_affected_columns = defaultdict(dict)
    error_type_counts = Counter()

    for counter, legacy_idx in enumerate(unmatched_legacy, start=1):
        legacy_row = legacy_df.loc[legacy_idx]

        candidate_indexes = generate_candidates(
            legacy_row,
            normalized_legacy,
            normalized_new,
            block_indexes,
            blocking_columns,
            used_new_indexes,
            config,
            key_index_new=key_index_new,
            user_key_positions=user_key_positions,
        )

        best_idx, best_score, second_best_score, status = find_best_candidate(
            legacy_row,
            new_df,
            candidate_indexes,
            config,
            id_positions,
            sparse_positions,
        )

        if status != "MATCHED":
            likely_skipped = (
                any(
                    str(legacy_row.iloc[p]).strip()
                    and str(legacy_row.iloc[p]).strip() in skipped_new_blob
                    for p in id_positions
                )
                if id_positions
                else False
            )

            unresolved_records.append(
                {
                    "legacy_index": legacy_idx,
                    "best_similarity": best_score,
                    "second_best_similarity": second_best_score,
                    "candidate_count": len(candidate_indexes),
                    "status": status,
                    "likely_cause": (
                        "possible counterpart skipped due to delimiter issue"
                        if likely_skipped
                        else ""
                    ),
                }
            )
            continue

        new_row = new_df.loc[best_idx]
        used_new_indexes.add(best_idx)

        differences = compare_rows_raw(
            legacy_row,
            new_row,
            legacy_columns,
            new_columns,
            config.null_token,
        )

        if not differences:
            matched_pairs.append(
                {
                    "legacy_index": legacy_idx,
                    "new_index": best_idx,
                    "match_type": "CANDIDATE_RAW_EQUAL",
                    "similarity": best_score,
                }
            )
            continue

        signature = difference_signature(differences)
        difference_signature_counts[signature] += 1

        for difference in differences:
            position = difference["position"]
            col_name = difference["legacy_column"] or f"Pos_{position}"
            column_mismatch_counts[position] += 1

            pattern = classify_difference(
                difference["legacy_value"],
                difference["new_value"],
            )

            error_type_counts[pattern] += 1
            error_type_affected_columns[pattern][(position, col_name)] = (
                error_type_affected_columns[pattern].get(
                    (position, col_name), 0
                )
                + 1
            )

            error_type_key = (position, pattern, "", "")
            column_error_type_counts[error_type_key] += 1

            if error_type_key not in samples_by_error_type:
                samples_by_error_type[error_type_key] = {
                    "legacy_index": legacy_idx,
                    "new_index": best_idx,
                    "position": position,
                    "legacy_column": difference["legacy_column"],
                    "new_column": difference["new_column"],
                    "legacy_value": difference["legacy_value"],
                    "new_value": difference["new_value"],
                    "similarity": best_score,
                    "legacy_full_row": legacy_row.tolist(),
                    "new_full_row": new_row.tolist(),
                    "pattern": pattern,
                }

        detailed_results.append(
            {
                "legacy_index": legacy_idx,
                "new_index": best_idx,
                "similarity": best_score,
                "difference_columns": " | ".join(
                    f"{x[0]}:{x[1]}" for x in signature
                ),
            }
        )

    new_only_records = [
        idx for idx in new_df.index if idx not in used_new_indexes
    ]
    actual_mismatch_count = len(detailed_results)

    summary = {
        "legacy_records": len(legacy_df),
        "new_records": len(new_df),
        "exact_raw_matches": sum(
            1 for pair in matched_pairs if pair["match_type"] == "EXACT_RAW"
        ),
        "candidate_raw_equal": sum(
            1
            for pair in matched_pairs
            if pair["match_type"] == "CANDIDATE_RAW_EQUAL"
        ),
        "mismatched_records": actual_mismatch_count,
        "unresolved_legacy_records": len(unresolved_records),
        "new_only_records": len(new_only_records),
        "legacy_encoding": config.legacy_encoding_label,
        "new_encoding": config.new_encoding_label,
        "encoding_mismatch": config.legacy_encoding_label.lower().replace(
            "-sig", ""
        )
        != config.new_encoding_label.lower().replace("-sig", ""),
    }

    return {
        "summary": summary,
        "matched_pairs": matched_pairs,
        "detailed_results": detailed_results,
        "column_mismatch_counts": column_mismatch_counts,
        "difference_signature_counts": difference_signature_counts,
        "unresolved_records": unresolved_records,
        "new_only_records": new_only_records,
        "blocking_columns": blocking_columns,
        "column_error_type_counts": column_error_type_counts,
        "samples_by_error_type": samples_by_error_type,
        "error_type_counts": error_type_counts,
        "error_type_affected_columns": error_type_affected_columns,
    }


# ============================================================
# CONSOLE SUMMARY PRINTING
# ============================================================


def print_report(results, structure_result):
    print("\n" + "=" * 80 + "\nCSV RECONCILIATION SUMMARY\n" + "=" * 80)
    summary = results["summary"]

    print(f"Legacy records               : {summary['legacy_records']:,}")
    print(f"New records                  : {summary['new_records']:,}")
    print(f"Exact raw matches            : {summary['exact_raw_matches']:,}")
    print(f"Candidate raw-equal          : {summary['candidate_raw_equal']:,}")
    print(f"Mismatched records           : {summary['mismatched_records']:,}")
    print(
        f"Unresolved legacy records    : {summary['unresolved_legacy_records']:,}"
    )
    print(f"New-only records             : {summary['new_only_records']:,}")

    print("\n" + "-" * 80 + "\nENCODING STATUS\n" + "-" * 80)
    print(f"Legacy file encoding         : {summary['legacy_encoding']}")
    print(f"New file encoding            : {summary['new_encoding']}")

    if summary["encoding_mismatch"]:
        print(
            "\n  *** ENCODING MISMATCH DETECTED *** (Decoded cleanly to Unicode)"
        )
    else:
        print("Encodings match.")

    print("\n" + "-" * 80 + "\nCOLUMN STRUCTURE STATUS\n" + "-" * 80)
    print(f"Legacy columns : {structure_result['legacy_column_count']}")
    print(f"New columns    : {structure_result['new_column_count']}")

    if structure_result["column_differences"]:
        print(
            f"Column name differences detected at {len(structure_result['column_differences'])} position(s)."
        )
    else:
        print("Column names match at all positions.")


# ============================================================
# NUMERIC & GROUPING IDENTIFICATION
# ============================================================


def _try_decimal(value: str) -> bool:
    try:
        d = Decimal(str(value).strip())
        return d.is_finite()
    except (InvalidOperation, TypeError, ValueError):
        try:
            f = float(str(value).strip())
            return math.isfinite(f)
        except (ValueError, TypeError):
            return False


_NUMERIC_THRESHOLD = 0.80


def identify_numeric_columns(df: pd.DataFrame, null_token: str) -> list:
    numeric_cols = []
    _parse_rates = {}

    for col in df.columns:
        non_empty = [
            v for v in df[col] if not is_null_like(v, null_token)
        ]

        if not non_empty:
            continue

        parsed_count = sum(1 for v in non_empty if _try_decimal(v))
        parse_rate = parsed_count / len(non_empty)

        if parse_rate < _NUMERIC_THRESHOLD:
            continue

        if _looks_like_id_col(col, non_empty):
            continue

        numeric_cols.append(col)
        _parse_rates[col] = parse_rate

    identify_numeric_columns._parse_rates = _parse_rates
    return numeric_cols


_ID_PATTERNS = _re.compile(
    r"(^|_)(id|key|code|num|no|number|ref|uuid|guid|seq|acc|sedol|isin|ssn)($|_)",
    _re.IGNORECASE,
)
_DATE_PATTERNS = _re.compile(
    r"(date|time|ts|timestamp|dt|year|month|day)",
    _re.IGNORECASE,
)
_DATE_VALUE_RE = _re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$")


def _looks_like_id_col(col: str, sample_values: list) -> bool:
    if _ID_PATTERNS.search(col):
        return True
    ints = 0
    for v in sample_values[:50]:
        try:
            int(v)
            ints += 1
        except (ValueError, TypeError):
            pass
    if ints > len(sample_values[:50]) * 0.8:
        return True
    return False


def _looks_like_date_col(col: str, sample_values: list) -> bool:
    if _DATE_PATTERNS.search(col):
        return True
    date_hits = sum(
        1 for v in sample_values[:20] if _DATE_VALUE_RE.match(str(v))
    )
    return date_hits > len(sample_values[:20]) * 0.5


def identify_grouping_columns(
    df: pd.DataFrame,
    numeric_cols: list,
    config: ReconciliationConfig,
) -> list:
    numeric_set = set(numeric_cols)
    grouping = []

    for col in df.columns:
        if col in numeric_set:
            continue

        non_empty = [
            v for v in df[col] if not is_null_like(v, config.null_token)
        ]
        if not non_empty:
            continue

        distinct = len({v.strip().lower() for v in non_empty})
        if distinct > config.max_grouping_cardinality:
            continue

        if _looks_like_id_col(col, non_empty) or _looks_like_date_col(
            col, non_empty
        ):
            continue

        grouping.append(col)

    return grouping


# ============================================================
# ROLLUP COMPUTATION
# ============================================================


def _safe_decimal(value: str):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        return None


def compute_rollups(
    legacy_df: pd.DataFrame,
    new_df: pd.DataFrame,
    numeric_cols: list,
    grouping_cols: list,
    null_token: str,
) -> dict:

    def _col_decimals(df, col):
        parsed = []
        skipped = 0
        for idx, v in df[col].items():
            if is_null_like(v, null_token):
                continue
            d = _safe_decimal(v)
            if d is not None:
                parsed.append((idx, d))
            else:
                skipped += 1
        return parsed, skipped

    def _summarise_numeric_frame(legacy_frame, new_frame):
        totals = {}
        for col in numeric_cols:
            legacy_vals, l_skip = _col_decimals(legacy_frame, col)
            new_vals, n_skip = _col_decimals(new_frame, col)

            l_sum = sum(d for _, d in legacy_vals)
            n_sum = sum(d for _, d in new_vals)
            l_count = len(legacy_vals)
            n_count = len(new_vals)

            totals[col] = {
                "legacy_sum": l_sum,
                "new_sum": n_sum,
                "legacy_count": l_count,
                "new_count": n_count,
                "legacy_skipped": l_skip,
                "new_skipped": n_skip,
                "sum_match": l_sum == n_sum,
            }
        return totals

    def _summarise_grouped_frame(legacy_frame, new_frame):
        grouped_totals = {}

        for g_col in grouping_cols:
            grouped_totals[g_col] = {}

            l_keys = (
                legacy_frame[g_col].astype(str).str.strip().str.lower()
            )
            n_keys = new_frame[g_col].astype(str).str.strip().str.lower()

            labels = {}
            for raw in list(legacy_frame[g_col]) + list(new_frame[g_col]):
                if is_null_like(raw, null_token):
                    continue
                t = str(raw).strip()
                labels.setdefault(t.lower(), t)

            all_keys = {
                str(k)
                for k in (set(l_keys.tolist()) | set(n_keys.tolist()))
                if k and str(k) != null_token.lower() and str(k) != "nan"
            }

            for g_key in sorted(all_keys):
                g_val = labels.get(g_key, g_key)
                legacy_mask = l_keys == g_key
                new_mask = n_keys == g_key

                legacy_sub = legacy_frame[legacy_mask]
                new_sub = new_frame[new_mask]

                col_stats = {}
                for n_col in numeric_cols:
                    lv, l_skip = _col_decimals(legacy_sub, n_col)
                    nv, n_skip = _col_decimals(new_sub, n_col)

                    l_sum = sum(d for _, d in lv)
                    n_sum = sum(d for _, d in nv)
                    l_count = len(lv)
                    n_count = len(nv)

                    col_stats[n_col] = {
                        "legacy_sum": l_sum,
                        "new_sum": n_sum,
                        "legacy_count": l_count,
                        "new_count": n_count,
                        "legacy_skipped": l_skip,
                        "new_skipped": n_skip,
                        "sum_match": l_sum == n_sum,
                    }

                grouped_totals[g_col][g_val] = col_stats

        return grouped_totals

    parse_rates = getattr(identify_numeric_columns, "_parse_rates", {})
    file_totals = _summarise_numeric_frame(legacy_df, new_df)
    grouped_totals = _summarise_grouped_frame(legacy_df, new_df)

    return {
        "numeric_columns": numeric_cols,
        "grouping_columns": grouping_cols,
        "parse_rates": parse_rates,
        "file_totals": file_totals,
        "grouped_totals": grouped_totals,
    }


def build_report_name(legacy_file: str, new_file: str) -> str:
    l = os.path.splitext(os.path.basename(legacy_file))[0]
    n = os.path.splitext(os.path.basename(new_file))[0]

    m = difflib.SequenceMatcher(None, l, n).find_longest_match(
        0, len(l), 0, len(n)
    )
    common = l[m.a : m.a + m.size].strip("_-. ")

    return f"{common or 'reconciliation'}_recon_report.csv"


# ============================================================
# EXPORT CSV REPORT
# ============================================================


def export_unified_csv(
    results,
    structure_result,
    config: ReconciliationConfig,
    legacy_df: pd.DataFrame,
    new_df: pd.DataFrame,
    legacy_eol,
    new_eol,
    rollup=None,
    legacy_duplicates: list[dict] = None,
    new_duplicates: list[dict] = None,
):
    out_dir = safe_path(config.output_directory, must_exist=False)
    os.makedirs(out_dir, exist_ok=True)

    out_path = os.path.join(
        out_dir,
        build_report_name(config.legacy_file, config.new_file),
    )

    summary = results["summary"]
    col_diffs = structure_result["column_differences"]
    legacy_cols = list(legacy_df.columns)
    new_cols = list(new_df.columns)

    samples_by_error_type = results["samples_by_error_type"]
    column_error_type_counts = results["column_error_type_counts"]
    error_type_counts = results.get("error_type_counts", {})
    error_type_affected_columns = results.get(
        "error_type_affected_columns", {}
    )

    rows = []
    rows.append(["=" * 60])
    rows.append(["RECONCILIATION RESULTS"])
    rows.append(["=" * 60])

    REPORT_SECTION_SPACING = 2

    def blank():
        for _ in range(REPORT_SECTION_SPACING):
            rows.append([])

    def section(title):
        blank()
        rows.append([f"### {title} ###"])
        blank()

    def summary_line(text):
        rows.append([f">> {text}"])

    # 1. ENCODING
    section("ENCODING")
    rows.append(["field", "value"])
    rows.append(["legacy_file", config.legacy_file])
    rows.append(["legacy_encoding", summary["legacy_encoding"]])
    rows.append(["new_file", config.new_file])
    rows.append(["new_encoding", summary["new_encoding"]])

    if summary["encoding_mismatch"]:
        summary_line(
            f"ENCODING MISMATCH: legacy={summary['legacy_encoding']} vs new={summary['new_encoding']}. "
            "Both files decoded to Unicode before comparison."
        )
    else:
        summary_line("Encodings match — no encoding difference detected.")

    # 2. SCHEMA MISMATCHES
    section("SCHEMA MISMATCHES (COLUMN NAME DIFFERENCES)")
    if not col_diffs:
        summary_line(
            "No schema differences — all column names match at every position."
        )
    else:
        legacy_name_list = ", ".join(
            d["legacy_column"] or "" for d in col_diffs
        )
        new_name_list = ", ".join(d["new_column"] or "" for d in col_diffs)

        summary_line(
            f"{len(col_diffs)} column name difference(s) found. "
            f"Legacy: [{legacy_name_list}] | New: [{new_name_list}]"
        )
        rows.append(["position", "legacy_column_name", "new_column_name"])
        for d in col_diffs:
            rows.append(
                [
                    d["position"],
                    d["legacy_column"] or "",
                    d["new_column"] or "",
                ]
            )

    # 3. COUNT MISMATCH
    section("COUNT MISMATCH")
    legacy_count = summary["legacy_records"]
    new_count = summary["new_records"]
    diff = legacy_count - new_count

    if diff == 0:
        summary_line(
            f"Row counts match — both files have {legacy_count:,} records."
        )
    else:
        summary_line(
            f"Row count difference of {abs(diff):,}: legacy={legacy_count:,}, new={new_count:,}."
        )

    rows.append(["metric", "legacy", "new"])
    rows.append(["total_records", legacy_count, new_count])
    rows.append(
        [
            "exact_matches",
            summary["exact_raw_matches"],
            summary["exact_raw_matches"],
        ]
    )
    rows.append(["mismatched_records", summary["mismatched_records"], ""])
    rows.append(
        ["unresolved_legacy", summary["unresolved_legacy_records"], ""]
    )
    rows.append(["new_only_records", "", summary["new_only_records"]])

    # 4. UNRESOLVED RECORDS
    section("UNRESOLVED RECORDS")
    unres = results["unresolved_records"]
    new_only = results["new_only_records"]

    if not unres and not new_only:
        summary_line("No unresolved records.")
    else:
        summary_line(
            f"{len(unres)} legacy record(s) could not be paired; "
            f"{len(new_only)} new record(s) remain unpaired."
        )

        blank()
        rows.append(["--- LEGACY (unresolved, as in file) ---"])
        rows.append(
            ["source", "file_row", "status", "best_similarity"] + legacy_cols
        )
        for r in unres:
            i = r["legacy_index"]
            rows.append(
                ["[LEGACY]", i + 2, r["status"], f"{r['best_similarity']:.2f}"]
                + legacy_df.loc[i].tolist()
            )

        blank()
        rows.append(["--- NEW (unpaired, as in file) ---"])
        rows.append(["source", "file_row"] + new_cols)
        for i in new_only:
            rows.append(["[NEW]", i + 2] + new_df.loc[i].tolist())

    # 4b. DUPLICATE ROWS
    section("DUPLICATE ROWS IN INPUT FILES")
    leg_dups = legacy_duplicates or []
    new_dups = new_duplicates or []

    if not leg_dups and not new_dups:
        summary_line("No duplicate rows detected in either file.")
    else:
        summary_line(
            f"{len(leg_dups)} duplicate row(s) identified in legacy file; "
            f"{len(new_dups)} duplicate row(s) identified in new file. "
            "All duplicate rows are fully reconciled."
        )

        blank()
        rows.append(["--- LEGACY DUPLICATES ---"])
        if leg_dups:
            rows.append(
                ["source", "file_row", "first_seen_file_row"] + legacy_cols
            )
            for d in leg_dups:
                rows.append(
                    ["[LEGACY_DUP]", d["file_row"], d["first_seen_file_row"]]
                    + d["values"]
                )
        else:
            rows.append(["(none)"])

        blank()
        rows.append(["--- NEW DUPLICATES ---"])
        if new_dups:
            rows.append(
                ["source", "file_row", "first_seen_file_row"] + new_cols
            )
            for d in new_dups:
                rows.append(
                    ["[NEW_DUP]", d["file_row"], d["first_seen_file_row"]]
                    + d["values"]
                )
        else:
            rows.append(["(none)"])

    # 5. BAD LINES
    section("SKIPPED LINES (delimiter count mismatch)")
    if not config.legacy_bad_lines and not config.new_bad_lines:
        summary_line("No rows skipped due to delimiter-count anomalies.")
    else:
        summary_line(
            f"{len(config.legacy_bad_lines or [])} legacy row(s), "
            f"{len(config.new_bad_lines or [])} new row(s) skipped."
        )

        def _bad_line_row(b):
            if isinstance(b, dict):
                return b.get("row", ""), b.get("raw_line", str(b))
            return "", ", ".join(str(x) for x in b)

        rows.append(["file", "row", "detail"])
        for b in config.legacy_bad_lines or []:
            row_no, detail = _bad_line_row(b)
            rows.append(["legacy", row_no, detail])
        for b in config.new_bad_lines or []:
            row_no, detail = _bad_line_row(b)
            rows.append(["new", row_no, detail])

    # 6. NEWLINE / EOL
    section("NEWLINE / EOL DIFFERENCES")
    legacy_file_eol = legacy_eol["file_eol"]
    new_file_eol = new_eol["file_eol"]
    eol_mismatch = legacy_file_eol != new_file_eol

    if eol_mismatch:
        summary_line(
            f"FILE-LEVEL EOL MISMATCH: legacy uses {legacy_file_eol}, new uses {new_file_eol}."
        )
    else:
        summary_line(f"File-level line endings match ({legacy_file_eol}).")

    rows.append(["source", "file_eol", "CRLF_count", "LF_count", "CR_count"])
    for label, eol in [("legacy", legacy_eol), ("new", new_eol)]:
        rows.append(
            [
                label,
                eol["file_eol"],
                eol["eol_counts"]["CRLF"],
                eol["eol_counts"]["LF"],
                eol["eol_counts"]["CR"],
            ]
        )

    # 7. MISMATCHED DATA
    section("MISMATCHED DATA")
    if not samples_by_error_type:
        summary_line("No data mismatches found.")
    else:
        summary_line(
            f"Summary of {len(error_type_counts)} distinct error type(s) "
            f"encountered across all mismatched columns:"
        )
        blank()
        rows.append(
            [
                "error_type",
                "total_occurrences",
                "affected_column_count",
                "affected_columns",
            ]
        )

        for err_type, total_occ in error_type_counts.most_common():
            cols_dict = error_type_affected_columns.get(err_type, {})
            sorted_cols = sorted(cols_dict.keys(), key=lambda x: x[0])
            formatted_cols = " | ".join(
                f"{pos}:{name} ({cols_dict[(pos, name)]})"
                for pos, name in sorted_cols
            )
            rows.append(
                [err_type, total_occ, len(sorted_cols), formatted_cols]
            )

        blank()
        summary_line("Detailed column-wise sample pairs:")

        by_position = defaultdict(list)
        for key in samples_by_error_type:
            position = key[0]
            by_position[position].append(key)

        positions_sorted = sorted(by_position.keys())

        for position in positions_sorted:
            keys_for_col = sorted(
                by_position[position],
                key=lambda k: (k[1], k[2], k[3]),
            )

            sample_0 = samples_by_error_type[keys_for_col[0]]
            col_legacy_name = sample_0["legacy_column"]
            col_new_name = sample_0["new_column"]

            col_label = (
                col_legacy_name
                if col_legacy_name == col_new_name
                else f"{col_legacy_name} / {col_new_name}"
            )

            type_count = len(keys_for_col)
            total_records = sum(
                column_error_type_counts[k] for k in keys_for_col
            )

            if position != positions_sorted[0]:
                blank()
            summary_line(
                f"Column '{col_label}' (position {position}): "
                f"{type_count} distinct error type(s) across "
                f"{total_records:,} mismatched record(s)."
            )

            for key in keys_for_col:
                position, pattern, legacy_val, new_val = key
                sample = samples_by_error_type[key]
                count = column_error_type_counts[key]

                blank()
                rows.append([col_label])
                label = (
                    f"error_type: {pattern} | "
                    f"first seen: legacy='{sample['legacy_value']}' vs new='{sample['new_value']}' "
                    f"| occurrences={count:,}"
                )
                rows.append([label])
                rows.append(["file"] + legacy_cols)

                legacy_full = sample["legacy_full_row"]
                rows.append(["[LEGACY]"] + list(legacy_full))
                rows.append(["[NEW]"] + list(sample["new_full_row"]))

    # 8. NUMERIC ROLLUP VALIDATION
    rollup = rollup or results.get("rollup")
    section("NUMERIC ROLLUP VALIDATION")

    if not rollup or not rollup["numeric_columns"]:
        summary_line("No numeric columns identified — rollup skipped.")
    else:
        num_cols = rollup["numeric_columns"]
        grp_cols = rollup["grouping_columns"]
        ft = rollup["file_totals"]
        gt = rollup.get("grouped_totals", {})
        parse_rates = rollup.get("parse_rates", {})

        col_summaries = [
            f"{col} ({parse_rates.get(col, 1.0):.0%} numeric)"
            for col in num_cols
        ]
        summary_line(
            f"Numeric columns identified ({len(num_cols)}): "
            + ", ".join(col_summaries)
        )

        if grp_cols:
            summary_line(
                f"Grouping columns identified ({len(grp_cols)}): "
                + ", ".join(grp_cols)
            )
        else:
            summary_line("No low-cardinality grouping columns identified.")

        # --- 8a. FILE-LEVEL ROLLUP ---
        blank()
        rows.append(["--- FILE-LEVEL ROLLUP ---"])

        mismatched_file_cols = [
            col for col in num_cols if not ft[col]["sum_match"]
        ]
        if mismatched_file_cols:
            summary_line(
                f"DIFFERENCES DETECTED IN FILE-LEVEL ROLLUPS FOR COLUMN(S): "
                + ", ".join(mismatched_file_cols)
            )
        else:
            summary_line("ALL FILE-LEVEL NUMERIC ROLLUPS MATCH PERFECTLY.")

        blank()

        header = ["metric"]
        for col in num_cols:
            header += [f"{col} [LEGACY]", f"{col} [NEW]"]
        rows.append(header)

        sum_row = ["SUM"]
        for col in num_cols:
            s = ft[col]
            match_flag = "" if s["sum_match"] else " !"
            sum_row += [
                str(s["legacy_sum"]) + match_flag,
                str(s["new_sum"]) + match_flag,
            ]
        rows.append(sum_row)

        cnt_row = ["COUNT (parsed)"]
        for col in num_cols:
            s = ft[col]
            cnt_row += [s["legacy_count"], s["new_count"]]
        rows.append(cnt_row)

        any_skipped = any(
            ft[col]["legacy_skipped"] > 0 or ft[col]["new_skipped"] > 0
            for col in num_cols
        )
        if any_skipped:
            skip_row = ["COUNT (skipped — non-numeric values)"]
            for col in num_cols:
                s = ft[col]
                skip_row += [s["legacy_skipped"], s["new_skipped"]]
            rows.append(skip_row)

        # --- 8b. COLUMN-BASED GROUPED ROLLUPS ---
        if grp_cols:
            blank()
            rows.append(["--- GROUPED ROLLUPS BY COLUMN ---"])

            # Map grouped mismatches per grouping column into a table structure
            grouped_mismatch_table = {}
            for g_col in grp_cols:
                grp_data = gt.get(g_col, {})
                diff_entries = []
                for g_val, col_stats in grp_data.items():
                    mismatched_cols = [
                        col
                        for col in num_cols
                        if not col_stats.get(col, {}).get("sum_match", True)
                    ]
                    if mismatched_cols:
                        cols_str = ", ".join(mismatched_cols)
                        diff_entries.append(f"{g_val} ({cols_str})")

                if diff_entries:
                    grouped_mismatch_table[g_col] = " | ".join(diff_entries)

            if grouped_mismatch_table:
                summary_line("DIFFERENCES DETECTED IN GROUPED ROLLUPS:")
                blank()
                rows.append(["grouping_column", "mismatched_values"])
                for g_col, m_vals in grouped_mismatch_table.items():
                    rows.append([g_col, m_vals])
            else:
                summary_line(
                    "ALL GROUPED ROLLUPS MATCH PERFECTLY ACROSS ALL CATEGORIES."
                )

            for g_col in grp_cols:
                blank()
                rows.append([f"Grouped by: {g_col}"])
                rows.append([])

                g_header = [g_col]
                for col in num_cols:
                    g_header += [f"{col} SUM [LEGACY]", f"{col} SUM [NEW]"]
                rows.append(g_header)

                grp_data = gt.get(g_col, {})
                for g_val in sorted(grp_data.keys()):
                    col_stats = grp_data[g_val]
                    data_row = [g_val]

                    for col in num_cols:
                        cs = col_stats.get(col, {})
                        sum_flag = (
                            "" if cs.get("sum_match", True) else " !"
                        )
                        data_row += [
                            str(cs.get("legacy_sum", "")) + sum_flag,
                            str(cs.get("new_sum", "")) + sum_flag,
                        ]

                    rows.append(data_row)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        for row in rows:
            writer.writerow([str(c) for c in row])

    print(f"\nUnified report written to: {out_path}")
    return out_path


# ============================================================
# MAIN ARGUMENT PARSING
# ============================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Reconcile a legacy CSV against a new CSV with adaptive ambiguity decision engine."
    )

    parser.add_argument("legacy_file", help="Path to the legacy CSV file")
    parser.add_argument("new_file", help="Path to the new/current CSV file")

    parser.add_argument(
        "--legacy-encoding", default="", dest="legacy_encoding"
    )
    parser.add_argument("--new-encoding", default="", dest="new_encoding")
    parser.add_argument("--delimiter", default="")
    parser.add_argument(
        "--output", default="reconciliation_output", dest="output_directory"
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.75,
        dest="min_similarity",
    )
    parser.add_argument(
        "--ambiguity-margin",
        type=float,
        default=0.05,
        dest="ambiguity_margin",
    )
    parser.add_argument(
        "--ambiguity-max-col-diff",
        type=float,
        default=1.0,
        dest="ambiguity_max_column_diff",
        help="Max column difference threshold to consider candidates ambiguous (default: 1.0 column difference)",
    )
    parser.add_argument(
        "--sparsity-threshold",
        type=float,
        default=0.70,
        dest="sparsity_threshold",
        help="Sparsity threshold to mask columns >= threshold empty during candidate evaluation (default: 0.70)",
    )
    parser.add_argument(
        "--max-block-columns",
        type=int,
        default=3,
        dest="max_block_columns",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=100,
        dest="max_candidates_per_row",
    )
    parser.add_argument(
        "--sample-limit", type=int, default=5, dest="sample_limit"
    )
    parser.add_argument("--quotechar", default='"')
    parser.add_argument(
        "--no-doublequote", action="store_false", dest="doublequote"
    )
    parser.add_argument(
        "--max-grouping-cardinality",
        type=int,
        default=20,
        dest="max_grouping_cardinality",
    )
    parser.add_argument(
        "--precision", type=int, default=2, dest="precision_digits"
    )
    parser.add_argument(
        "--keys",
        nargs="+",
        default=[],
        dest="user_keys",
        help="Explicit key column name(s) to match rows on strictly (e.g. --keys ID Account_No)",
    )

    parser.set_defaults(doublequote=True)
    return parser.parse_args()


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":

    args = parse_args()

    config = ReconciliationConfig(
        precision_digits=args.precision_digits,
        legacy_file=args.legacy_file,
        legacy_encoding=args.legacy_encoding,
        new_file=args.new_file,
        new_encoding=args.new_encoding,
        delimiter=args.delimiter,
        min_similarity=args.min_similarity,
        ambiguity_margin=args.ambiguity_margin,
        ambiguity_max_column_diff=args.ambiguity_max_column_diff,
        sparsity_threshold=args.sparsity_threshold,
        max_block_columns=args.max_block_columns,
        max_candidates_per_row=args.max_candidates_per_row,
        sample_limit=args.sample_limit,
        output_directory=args.output_directory,
        quotechar=args.quotechar,
        doublequote=args.doublequote,
        max_grouping_cardinality=args.max_grouping_cardinality,
        user_keys=args.user_keys,
    )

    _PRECISION_DIGITS = config.precision_digits

    print("\nLoading CSV files...")
    legacy_df, new_df = load_csv(config)

    print("\nAuditing duplicate rows...")
    legacy_duplicates = audit_duplicate_rows(
        legacy_df, config.null_token, "legacy"
    )
    new_duplicates = audit_duplicate_rows(new_df, config.null_token, "new")

    print(
        f"  legacy shape: {legacy_df.shape} ({len(legacy_duplicates)} duplicates logged)"
    )
    print(
        f"  new    shape: {new_df.shape} ({len(new_duplicates)} duplicates logged)"
    )

    structure_result = validate_structure(legacy_df, new_df)

    if not structure_result["column_count_match"]:
        common = min(
            structure_result["legacy_column_count"],
            structure_result["new_column_count"],
        )
        print(
            f"\nWARNING: column count differs "
            f"(legacy={structure_result['legacy_column_count']}, "
            f"new={structure_result['new_column_count']}). "
            f"Comparing first {common} columns only."
        )
        legacy_df = legacy_df.iloc[:, :common]
        new_df = new_df.iloc[:, :common]

    print("\nStarting reconciliation...")
    results = reconcile(legacy_df, new_df, config)

    print("\nDetecting line endings...")
    legacy_eol = detect_eol(
        config.legacy_file, config.legacy_encoding, config.legacy_delimiter
    )
    new_eol = detect_eol(
        config.new_file, config.new_encoding, config.new_delimiter
    )

    print_report(results, structure_result)

    print("\nIdentifying numeric and grouping columns...")
    numeric_cols = identify_numeric_columns(legacy_df, config.null_token)
    grouping_cols = identify_grouping_columns(
        legacy_df, numeric_cols, config
    )

    new_df_for_rollup = new_df.copy()
    new_df_for_rollup.columns = legacy_df.columns

    rollup = compute_rollups(
        legacy_df,
        new_df_for_rollup,
        numeric_cols,
        grouping_cols,
        config.null_token,
    )

    out_path = export_unified_csv(
        results,
        structure_result,
        config,
        legacy_df,
        new_df,
        legacy_eol,
        new_eol,
        rollup=rollup,
        legacy_duplicates=legacy_duplicates,
        new_duplicates=new_duplicates,
    )

    print("\nReconciliation completed successfully.")