from __future__ import annotations

import hashlib
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import chardet
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

@dataclass
class ReconciliationConfig:

    legacy_file: str
    new_file: str

    delimiter: str = ","

    # Per-file encodings.  legacy_encoding / new_encoding
    # take precedence; encoding is the shared fallback used
    # when neither per-file value is supplied.
    encoding: str = "utf-8"
    legacy_encoding: str = ""
    new_encoding: str = ""

    # Quote handling.
    # quotechar  : character used to wrap field values.
    # doublequote: when True, "" inside a quoted field is
    #              interpreted as a literal ".
    quotechar: str = '"'
    doublequote: bool = True

    # --------------------------------------------------------
    # Candidate matching
    # --------------------------------------------------------

    # Minimum normalized similarity required for a candidate.
    min_similarity: float = 0.80

    # Best candidate must beat second-best candidate by this
    # margin. Otherwise record is considered ambiguous.
    ambiguity_margin: float = 0.05

    # Maximum number of blocking columns to use together.
    max_block_columns: int = 3

    # Minimum uniqueness ratio for a column to be considered
    # useful for blocking.
    min_uniqueness_ratio: float = 0.01

    # Maximum fraction of rows that a single blocking value
    # should represent.
    max_block_frequency: float = 0.10

    # Maximum candidate rows to evaluate for one legacy row.
    max_candidates_per_row: int = 100

    # --------------------------------------------------------
    # Reporting
    # --------------------------------------------------------

    sample_limit: int = 5

    output_directory: str = "reconciliation_output"

    # Internal representation for NULL.
    null_token: str = "<NULL>"

    # Maximum number of distinct values a column may have
    # to be considered a grouping (low-cardinality) column
    # for the grouped rollup section.
    max_grouping_cardinality: int = 20


# ============================================================
# ENCODING DETECTION
# ============================================================

# Number of bytes read from the file for chardet detection.
# 64 KB is enough for chardet to be confident on virtually
# all real-world CSV files, including UTF-16 (which has a
# BOM in the first two bytes that makes detection instant).
_DETECT_SAMPLE_BYTES = 65_536


def detect_encoding(
    filepath: str,
) -> tuple[str, float]:

    """
    Sniff the encoding of a file using chardet.

    Returns:
        (encoding_name, confidence)  e.g. ("utf-16", 1.0)

    Falls back to "utf-8" with 0.0 confidence if chardet
    cannot make a determination.
    """

    with open(filepath, "rb") as f:
        raw = f.read(_DETECT_SAMPLE_BYTES)

    result = chardet.detect(raw)

    encoding = (
        result.get("encoding")
        or "utf-8"
    )

    confidence = (
        result.get("confidence")
        or 0.0
    )

    return encoding, confidence


def resolve_encoding(
    explicit: str,
    filepath: str,
    label: str,
) -> tuple[str, str]:

    """
    Return (encoding_to_use, detection_source).

    detection_source is one of:
        "explicit"   – caller supplied a value
        "detected"   – chardet inferred it
        "default"    – chardet returned nothing; utf-8 assumed

    Prints a one-line status so the operator can see what
    was used for each file.
    """

    if explicit:

        print(
            f"  {label}: {explicit} "
            f"(explicit)"
        )

        return explicit, "explicit"

    detected, confidence = detect_encoding(
        filepath
    )

    if confidence > 0.0:

        print(
            f"  {label}: {detected} "
            f"(auto-detected, "
            f"confidence={confidence:.0%})"
        )

        return detected, "detected"

    print(
        f"  {label}: utf-8 "
        f"(chardet inconclusive, defaulting)"
    )

    return "utf-8", "default"




# ============================================================
# EOL / NEWLINE DETECTION
# ============================================================

def detect_eol(filepath: str, encoding: str) -> dict:
    """
    Detect both file-level line endings and any embedded
    newlines inside field values.

    Returns:
        {
          "file_eol"   : "CRLF" | "LF" | "CR" | "MIXED" | "NONE",
          "eol_counts" : {"CRLF": n, "LF": n, "CR": n},
          "embedded_newline_rows": [
              {"row": row_number_1based, "col": col_name, "value": raw_value},
              ...
          ],
        }

    File-level detection reads raw bytes so it works regardless
    of how pandas decodes the file.
    """

    # ---- file-level line endings (raw bytes) ----------------

    with open(filepath, "rb") as f:
        raw = f.read()

    crlf_count = raw.count(b"\r\n")
    # Pure CR: \r not followed by \n
    cr_count   = sum(
        1 for i, b in enumerate(raw)
        if b == ord(b"\r")
        and (i + 1 >= len(raw) or raw[i + 1] != ord(b"\n"))
    )
    # Pure LF: \n not preceded by \r
    lf_count   = sum(
        1 for i, b in enumerate(raw)
        if b == ord(b"\n")
        and (i == 0 or raw[i - 1] != ord(b"\r"))
    )

    eol_counts = {"CRLF": crlf_count, "LF": lf_count, "CR": cr_count}

    active = [k for k, v in eol_counts.items() if v > 0]

    if   len(active) == 0: file_eol = "NONE"
    elif len(active) == 1: file_eol = active[0]
    else:                  file_eol = "MIXED"

    # ---- embedded newlines inside field values ---------------
    # Re-read through pandas so quoted multiline fields are
    # handled correctly.

    embedded = []

    try:
        df = pd.read_csv(
            filepath,
            encoding=encoding,
            dtype=str,
            keep_default_na=False,
        )

        for col in df.columns:
            for row_idx, val in df[col].items():
                if isinstance(val, str) and (
                    "\n" in val or "\r" in val
                ):
                    embedded.append(
                        {
                            "row":   row_idx + 2,   # 1-based + header
                            "col":   col,
                            "value": repr(val),
                        }
                    )

    except Exception:
        # If re-read fails, skip embedded scan gracefully.
        pass

    return {
        "file_eol":              file_eol,
        "eol_counts":            eol_counts,
        "embedded_newline_rows": embedded,
    }

# ============================================================
# CSV LOADING
# ============================================================

def load_csv(config: ReconciliationConfig):

    """
    Load CSVs as strings.

    IMPORTANT:
    dtype=str + keep_default_na=False preserves the textual
    representation from the CSV as much as possible.

    Therefore:

        1000
        1000.00
        001000

    remain distinguishable.
    """

    # Resolve per-file encodings.
    # If the caller supplied an explicit value it is used as-is.
    # Otherwise chardet sniffs the file and the result is written
    # back into config so that reconcile() and the summary always
    # report the encoding that was actually used — not the blank
    # placeholder the user left unset.
    print("\nResolving file encodings:")

    legacy_enc, _ = resolve_encoding(
        config.legacy_encoding,
        config.legacy_file,
        "legacy",
    )

    new_enc, _ = resolve_encoding(
        config.new_encoding,
        config.new_file,
        "new   ",
    )

    # Write back so the rest of the pipeline (reconcile,
    # print_report, summary.csv) sees the resolved values.
    config.legacy_encoding = legacy_enc
    config.new_encoding = new_enc

    legacy_df = pd.read_csv(
        config.legacy_file,
        delimiter=config.delimiter,
        encoding=legacy_enc,
        dtype=str,
        keep_default_na=False,
        # Quote handling: strip enclosing quotes from both
        # field values and column headers without altering
        # the inner content.
        quotechar=config.quotechar,
        doublequote=config.doublequote,
        quoting=0,              # csv.QUOTE_MINIMAL
    )

    new_df = pd.read_csv(
        config.new_file,
        delimiter=config.delimiter,
        encoding=new_enc,
        dtype=str,
        keep_default_na=False,
        quotechar=config.quotechar,
        doublequote=config.doublequote,
        quoting=0,              # csv.QUOTE_MINIMAL
    )

    return legacy_df, new_df


# ============================================================
# STRUCTURAL VALIDATION
# ============================================================

def validate_structure(
    legacy_df: pd.DataFrame,
    new_df: pd.DataFrame,
):
    """
    Column POSITION is authoritative.

    Column-name differences are reported but do not prevent
    comparison, provided column counts are identical.
    """

    legacy_columns = list(legacy_df.columns)
    new_columns = list(new_df.columns)

    result = {
        "legacy_column_count": len(legacy_columns),
        "new_column_count": len(new_columns),
        "column_count_match":
            len(legacy_columns) == len(new_columns),
        "column_differences": [],
    }

    max_columns = max(
        len(legacy_columns),
        len(new_columns),
    )

    for position in range(max_columns):

        legacy_name = (
            legacy_columns[position]
            if position < len(legacy_columns)
            else None
        )

        new_name = (
            new_columns[position]
            if position < len(new_columns)
            else None
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
# RAW VALUE
# ============================================================

def raw_value(
    value: Any,
    null_token: str,
) -> str:

    """
    Return the ORIGINAL comparison representation.

    NO:
        strip()
        lower()
        numeric conversion
        date conversion

    happens here.
    """

    if value is None:
        return null_token

    if pd.isna(value):
        return null_token

    return str(value)


# ============================================================
# MATCHING VALUE
# ============================================================

def matching_value(
    value: Any,
    null_token: str,
) -> str:

    """
    Used ONLY for candidate discovery/blocking.

    It does NOT determine the final match.

    Example:

        "Ravi "
        "ravi"

    may be treated as similar for candidate discovery.

    During final comparison:

        "Ravi " != "ravi"
    """

    if value is None or pd.isna(value):
        return null_token

    return str(value).strip().lower()


# ============================================================
# DATAFRAME MATCHING REPRESENTATION
# ============================================================

def build_matching_dataframe(
    df: pd.DataFrame,
    null_token: str,
):

    """
    Build a separate normalized representation.

    The ORIGINAL dataframe is never modified.
    """

    result = pd.DataFrame(index=df.index)

    for column in df.columns:

        result[column] = df[column].map(
            lambda x: matching_value(
                x,
                null_token,
            )
        )

    return result


# ============================================================
# ROW FINGERPRINT
# ============================================================

def row_fingerprint(
    row: pd.Series,
    null_token: str,
):

    values = [
        raw_value(
            value,
            null_token,
        )
        for value in row.tolist()
    ]

    payload = "\x1f".join(values)

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


# ============================================================
# EXACT RAW MATCHING
# ============================================================

def find_exact_raw_matches(
    legacy_df: pd.DataFrame,
    new_df: pd.DataFrame,
    config: ReconciliationConfig,
):

    """
    First pass.

    Finds rows that are completely identical in their RAW
    representation.

    Row order does not matter.

    Duplicate rows are handled correctly.
    """

    new_hash_map = defaultdict(list)

    for new_idx, row in new_df.iterrows():

        fingerprint = row_fingerprint(
            row,
            config.null_token,
        )

        new_hash_map[fingerprint].append(
            new_idx
        )

    matched_pairs = []

    used_new = set()

    unmatched_legacy = []

    for legacy_idx, row in legacy_df.iterrows():

        fingerprint = row_fingerprint(
            row,
            config.null_token,
        )

        candidates = new_hash_map.get(
            fingerprint,
            []
        )

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

            unmatched_legacy.append(
                legacy_idx
            )

    unmatched_new = [
        idx
        for idx in new_df.index
        if idx not in used_new
    ]

    return (
        matched_pairs,
        unmatched_legacy,
        unmatched_new,
    )


# ============================================================
# SELECT BLOCKING COLUMNS
# ============================================================

def select_blocking_columns(
    normalized_df: pd.DataFrame,
    config: ReconciliationConfig,
):
    """
    Automatically identify useful columns for candidate
    generation.

    A useful blocking column should:

    1. Have reasonable uniqueness.
    2. Not have one value appearing across most records.

    NOTE:
    These are NOT business keys.

    They are only used to reduce the search space.
    """

    candidates = []

    row_count = len(normalized_df)

    if row_count == 0:
        return candidates

    for position, column in enumerate(
        normalized_df.columns
    ):

        value_counts = (
            normalized_df[column]
            .value_counts(dropna=False)
        )

        unique_count = len(value_counts)

        uniqueness_ratio = (
            unique_count / row_count
        )

        largest_frequency = (
            value_counts.iloc[0] / row_count
            if len(value_counts)
            else 1.0
        )

        if (
            uniqueness_ratio
            >= config.min_uniqueness_ratio
            and
            largest_frequency
            <= config.max_block_frequency
        ):

            candidates.append(
                {
                    "position": position,
                    "column": column,
                    "uniqueness_ratio":
                        uniqueness_ratio,
                    "largest_frequency":
                        largest_frequency,
                }
            )

    # Prefer highly unique columns.
    candidates.sort(
        key=lambda x:
            x["uniqueness_ratio"],
        reverse=True,
    )

    return candidates


# ============================================================
# BUILD BLOCK INDEX
# ============================================================

def build_block_indexes(
    normalized_new_df: pd.DataFrame,
    blocking_columns,
):
    """
    Build lookup indexes for single and multi-column
    combinations.

    Example:

        column 2
        column 5
        column 2 + column 5

    ->

        value -> [row indexes]
    """

    indexes = {}

    column_positions = [
        item["position"]
        for item in blocking_columns
    ]

    max_size = min(
        3,
        len(column_positions),
    )

    for size in range(
        1,
        max_size + 1,
    ):

        for combo in combinations(
            column_positions,
            size,
        ):

            lookup = defaultdict(list)

            for idx, row in normalized_new_df.iterrows():

                key = tuple(
                    row.iloc[position]
                    for position in combo
                )

                lookup[key].append(idx)

            indexes[combo] = lookup

    return indexes


# ============================================================
# GENERATE CANDIDATES
# ============================================================

def generate_candidates(
    legacy_row: pd.Series,
    normalized_legacy_df: pd.DataFrame,
    normalized_new_df: pd.DataFrame,
    block_indexes,
    blocking_columns,
    used_new_indexes,
    config: ReconciliationConfig,
):
    """
    Generate candidate New rows using blocking.

    The strongest available block is attempted first.

    This avoids comparing the legacy row against the entire
    New dataframe.
    """

    candidate_scores = Counter()

    legacy_idx = legacy_row.name

    normalized_legacy_row = (
        normalized_legacy_df.loc[legacy_idx]
    )

    column_positions = [
        item["position"]
        for item in blocking_columns
    ]

    max_size = min(
        config.max_block_columns,
        len(column_positions),
    )

    # Start with strongest / larger combinations.
    for size in range(
        max_size,
        0,
        -1,
    ):

        for combo in combinations(
            column_positions,
            size,
        ):

            lookup = block_indexes.get(
                combo
            )

            if lookup is None:
                continue

            key = tuple(
                normalized_legacy_row.iloc[position]
                for position in combo
            )

            matches = lookup.get(
                key,
                []
            )

            for new_idx in matches:

                if new_idx not in used_new_indexes:

                    candidate_scores[
                        new_idx
                    ] += size

        # Once we have candidates from a strong
        # multi-column block, don't unnecessarily
        # open up the entire search space.
        if candidate_scores:

            if size >= 2:

                break

    candidates = list(
        candidate_scores.keys()
    )

    # Safety valve.
    if len(candidates) > config.max_candidates_per_row:

        candidates.sort(
            key=lambda idx:
                candidate_scores[idx],
            reverse=True,
        )

        candidates = candidates[
            :config.max_candidates_per_row
        ]

    return candidates


# ============================================================
# ROW SIMILARITY
# ============================================================

def row_similarity(
    legacy_row: pd.Series,
    new_row: pd.Series,
    config: ReconciliationConfig,
):
    """
    Calculate normalized similarity.

    ONLY used to rank candidates.

    It does NOT determine whether the values match.
    """

    total_columns = len(
        legacy_row
    )

    if total_columns == 0:
        return 0.0

    matches = 0

    for legacy_value, new_value in zip(
        legacy_row.tolist(),
        new_row.tolist(),
    ):

        if (
            matching_value(
                legacy_value,
                config.null_token,
            )
            ==
            matching_value(
                new_value,
                config.null_token,
            )
        ):

            matches += 1

    return (
        matches /
        total_columns
    )


# ============================================================
# BEST CANDIDATE
# ============================================================

def find_best_candidate(
    legacy_row: pd.Series,
    new_df: pd.DataFrame,
    candidate_indexes,
    config: ReconciliationConfig,
):
    """
    Evaluate only blocked candidates.

    Returns:

        best index
        best similarity
        second-best similarity
        status
    """

    if not candidate_indexes:

        return (
            None,
            0.0,
            0.0,
            "UNRESOLVED",
        )

    scores = []

    for new_idx in candidate_indexes:

        new_row = new_df.loc[
            new_idx
        ]

        score = row_similarity(
            legacy_row,
            new_row,
            config,
        )

        scores.append(
            (
                new_idx,
                score,
            )
        )

    scores.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    best_idx, best_score = scores[0]

    second_best_score = (
        scores[1][1]
        if len(scores) > 1
        else 0.0
    )

    if (
        best_score
        < config.min_similarity
    ):

        return (
            None,
            best_score,
            second_best_score,
            "UNRESOLVED",
        )

    if (
        len(scores) > 1
        and
        (
            best_score
            - second_best_score
        )
        < config.ambiguity_margin
    ):

        return (
            None,
            best_score,
            second_best_score,
            "AMBIGUOUS",
        )

    return (
        best_idx,
        best_score,
        second_best_score,
        "MATCHED",
    )


# ============================================================
# RAW COLUMN COMPARISON
# ============================================================

def compare_rows_raw(
    legacy_row: pd.Series,
    new_row: pd.Series,
    legacy_columns,
    new_columns,
    null_token: str,
):
    """
    FINAL comparison.

    RAW values only.

    No trimming.
    No lowercase.
    No date conversion.
    No numeric conversion.

    Therefore every representation difference remains
    a mismatch.
    """

    differences = []

    for position in range(
        len(legacy_columns)
    ):

        legacy_value = raw_value(
            legacy_row.iloc[position],
            null_token,
        )

        new_value = raw_value(
            new_row.iloc[position],
            null_token,
        )

        if legacy_value != new_value:

            differences.append(
                {
                    "position":
                        position + 1,

                    "legacy_column":
                        legacy_columns[position],

                    "new_column":
                        new_columns[position],

                    "legacy_value":
                        legacy_value,

                    "new_value":
                        new_value,
                }
            )

    return differences


# ============================================================
# DIFFERENCE SIGNATURE
# ============================================================

def difference_signature(
    differences
):
    """
    Creates:

        ("AMOUNT",)

    or:

        ("AMOUNT", "STATUS")

    based on COLUMN POSITION.

    Position is included to avoid problems when names differ.
    """

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
    """
    Complete reconciliation pipeline.
    """

    legacy_columns = list(
        legacy_df.columns
    )

    new_columns = list(
        new_df.columns
    )

    # --------------------------------------------------------
    # Matching representation
    # --------------------------------------------------------

    normalized_legacy = (
        build_matching_dataframe(
            legacy_df,
            config.null_token,
        )
    )

    normalized_new = (
        build_matching_dataframe(
            new_df,
            config.null_token,
        )
    )

    # --------------------------------------------------------
    # PASS 1: Exact raw matching
    # --------------------------------------------------------

    (
        matched_pairs,
        unmatched_legacy,
        unmatched_new,
    ) = find_exact_raw_matches(
        legacy_df,
        new_df,
        config,
    )

    used_new_indexes = {
        pair["new_index"]
        for pair in matched_pairs
    }

    # --------------------------------------------------------
    # Blocking column selection
    # --------------------------------------------------------

    blocking_columns = (
        select_blocking_columns(
            normalized_new,
            config,
        )
    )

    print("\nSelected blocking columns:")

    for item in blocking_columns:

        print(
            f"  Position {item['position'] + 1}: "
            f"{item['column']} "
            f"(uniqueness="
            f"{item['uniqueness_ratio']:.2%})"
        )

    # --------------------------------------------------------
    # Build block indexes
    # --------------------------------------------------------

    block_indexes = (
        build_block_indexes(
            normalized_new,
            blocking_columns,
        )
    )

    # --------------------------------------------------------
    # Results
    # --------------------------------------------------------

    detailed_results = []

    unresolved_records = []

    column_mismatch_counts = Counter()

    difference_signature_counts = Counter()

    samples_by_column = defaultdict(list)

    samples_by_signature = defaultdict(list)

    # Per (position, legacy_value, new_value) error type.
    # Exactly 1 sample row pair stored per distinct type.
    column_error_type_counts = Counter()

    samples_by_error_type = {}

    # --------------------------------------------------------
    # PASS 2: Candidate matching
    # --------------------------------------------------------

    for counter, legacy_idx in enumerate(
        unmatched_legacy,
        start=1,
    ):

        legacy_row = legacy_df.loc[
            legacy_idx
        ]

        candidate_indexes = (
            generate_candidates(
                legacy_row,
                normalized_legacy,
                normalized_new,
                block_indexes,
                blocking_columns,
                used_new_indexes,
                config,
            )
        )

        (
            best_idx,
            best_score,
            second_best_score,
            status,
        ) = find_best_candidate(
            legacy_row,
            new_df,
            candidate_indexes,
            config,
        )

        # ----------------------------------------------------
        # No reliable candidate
        # ----------------------------------------------------

        if status != "MATCHED":

            unresolved_records.append(
                {
                    "legacy_index":
                        legacy_idx,

                    "best_similarity":
                        best_score,

                    "second_best_similarity":
                        second_best_score,

                    "candidate_count":
                        len(candidate_indexes),

                    "status":
                        status,
                }
            )

            continue

        # ----------------------------------------------------
        # Candidate selected
        # ----------------------------------------------------

        new_row = new_df.loc[
            best_idx
        ]

        used_new_indexes.add(
            best_idx
        )

        # ----------------------------------------------------
        # FINAL RAW COMPARISON
        # ----------------------------------------------------

        differences = compare_rows_raw(
            legacy_row,
            new_row,
            legacy_columns,
            new_columns,
            config.null_token,
        )

        # Candidate unexpectedly has no raw differences.
        # This can happen when matching representation was
        # normalized but the raw values happen to be equal.
        if not differences:

            matched_pairs.append(
                {
                    "legacy_index":
                        legacy_idx,

                    "new_index":
                        best_idx,

                    "match_type":
                        "CANDIDATE_RAW_EQUAL",

                    "similarity":
                        best_score,
                }
            )

            continue

        # ----------------------------------------------------
        # Difference signature
        # ----------------------------------------------------

        signature = (
            difference_signature(
                differences
            )
        )

        difference_signature_counts[
            signature
        ] += 1

        # ----------------------------------------------------
        # Column-level statistics
        # Each distinct (position, legacy_value, new_value)
        # triple is a separate error type.
        # Only 1 sample row pair is kept per error type.
        # ----------------------------------------------------

        for difference in differences:

            position = (
                difference["position"]
            )

            column_mismatch_counts[
                position
            ] += 1

            # Key that uniquely identifies this error type:
            # same column AND same pair of values.
            error_type_key = (
                position,
                difference["legacy_value"],
                difference["new_value"],
            )

            # Track count per error type
            column_error_type_counts[
                error_type_key
            ] += 1

            # Store exactly 1 sample per error type
            if error_type_key not in samples_by_error_type:

                samples_by_error_type[
                    error_type_key
                ] = {
                    "legacy_index":
                        legacy_idx,

                    "new_index":
                        best_idx,

                    "position":
                        position,

                    "legacy_column":
                        difference[
                            "legacy_column"
                        ],

                    "new_column":
                        difference[
                            "new_column"
                        ],

                    "legacy_value":
                        difference[
                            "legacy_value"
                        ],

                    "new_value":
                        difference[
                            "new_value"
                        ],

                    "similarity":
                        best_score,

                    # Full rows for the unified CSV report
                    "legacy_full_row":
                        legacy_row.tolist(),

                    "new_full_row":
                        new_row.tolist(),
                }

        # ----------------------------------------------------
        # Signature samples
        # ----------------------------------------------------

        if len(
            samples_by_signature[
                signature
            ]
        ) < config.sample_limit:

            samples_by_signature[
                signature
            ].append(
                {
                    "legacy_index":
                        legacy_idx,

                    "new_index":
                        best_idx,

                    "similarity":
                        best_score,

                    "differences":
                        differences,
                }
            )

        # ----------------------------------------------------
        # Detailed result
        # ----------------------------------------------------

        detailed_results.append(
            {
                "legacy_index":
                    legacy_idx,

                "new_index":
                    best_idx,

                "similarity":
                    best_score,

                "difference_columns":
                    " | ".join(
                        f"{x[0]}:{x[1]}"
                        for x in signature
                    ),
            }
        )

    # --------------------------------------------------------
    # New-only records
    # --------------------------------------------------------

    new_only_records = [
        idx
        for idx in new_df.index
        if idx not in used_new_indexes
    ]

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    actual_mismatch_count = len(
        detailed_results
    )

    # config.legacy_encoding / config.new_encoding are already
    # resolved by load_csv (either the caller's explicit value,
    # or the chardet-detected encoding written back in-place).
    # No further fallback logic is needed here.
    legacy_enc_used = config.legacy_encoding
    new_enc_used    = config.new_encoding

    summary = {
        "legacy_records":
            len(legacy_df),

        "new_records":
            len(new_df),

        "exact_raw_matches":
            sum(
                1
                for pair in matched_pairs
                if pair["match_type"]
                == "EXACT_RAW"
            ),

        "candidate_raw_equal":
            sum(
                1
                for pair in matched_pairs
                if pair["match_type"]
                == "CANDIDATE_RAW_EQUAL"
            ),

        "mismatched_records":
            actual_mismatch_count,

        "unresolved_legacy_records":
            len(unresolved_records),

        "new_only_records":
            len(new_only_records),

        # Encoding metadata.
        # encoding_mismatch is True whenever the two files
        # were read with different encodings.  Comparison
        # is still valid because both DataFrames hold plain
        # Python Unicode strings after decoding, but the
        # flag is surfaced so reviewers are aware.
        "legacy_encoding":
            legacy_enc_used,

        "new_encoding":
            new_enc_used,

        "encoding_mismatch":
            legacy_enc_used.lower()
            != new_enc_used.lower(),
    }

    return {
        "summary":
            summary,

        "matched_pairs":
            matched_pairs,

        "detailed_results":
            detailed_results,

        "column_mismatch_counts":
            column_mismatch_counts,

        "difference_signature_counts":
            difference_signature_counts,

        "samples_by_column":
            samples_by_column,

        "samples_by_signature":
            samples_by_signature,

        "unresolved_records":
            unresolved_records,

        "new_only_records":
            new_only_records,

        "blocking_columns":
            blocking_columns,

        "column_error_type_counts":
            column_error_type_counts,

        "samples_by_error_type":
            samples_by_error_type,
    }


# ============================================================
# REPORT
# ============================================================

def print_report(
    results,
    structure_result,
):

    print("\n")
    print("=" * 80)
    print("CSV RECONCILIATION REPORT")
    print("=" * 80)

    summary = results[
        "summary"
    ]

    print(
        f"Legacy records              : "
        f"{summary['legacy_records']:,}"
    )

    print(
        f"New records                 : "
        f"{summary['new_records']:,}"
    )

    print(
        f"Exact raw matches           : "
        f"{summary['exact_raw_matches']:,}"
    )

    print(
        f"Candidate raw-equal         : "
        f"{summary['candidate_raw_equal']:,}"
    )

    print(
        f"Mismatched records          : "
        f"{summary['mismatched_records']:,}"
    )

    print(
        f"Unresolved legacy records   : "
        f"{summary['unresolved_legacy_records']:,}"
    )

    print(
        f"New-only records            : "
        f"{summary['new_only_records']:,}"
    )

    # --------------------------------------------------------
    # Encoding
    # --------------------------------------------------------

    print("\n")
    print("-" * 80)
    print("ENCODING")
    print("-" * 80)

    print(
        f"Legacy file encoding        : "
        f"{summary['legacy_encoding']}"
    )

    print(
        f"New file encoding           : "
        f"{summary['new_encoding']}"
    )

    if summary["encoding_mismatch"]:

        print(
            "\n  *** ENCODING MISMATCH DETECTED ***\n"
            "  The two files were read with different "
            "encodings.\n"
            "  Comparison remains valid — both files are\n"
            "  decoded to Unicode before any comparison —\n"
            "  but verify this was intentional."
        )

    else:

        print(
            "Encodings match."
        )

    # --------------------------------------------------------
    # Column structure
    # --------------------------------------------------------

    print("\n")
    print("-" * 80)
    print("COLUMN STRUCTURE")
    print("-" * 80)

    print(
        f"Legacy columns : "
        f"{structure_result['legacy_column_count']}"
    )

    print(
        f"New columns    : "
        f"{structure_result['new_column_count']}"
    )

    if structure_result[
        "column_differences"
    ]:

        print("\nCOLUMN NAME DIFFERENCES:")

        for difference in structure_result[
            "column_differences"
        ]:

            print(
                f"Position "
                f"{difference['position']}: "
                f"{difference['legacy_column']} "
                f"<-> "
                f"{difference['new_column']}"
            )

    else:

        print(
            "Column names match at all positions."
        )

    # --------------------------------------------------------
    # Column mismatches
    # --------------------------------------------------------

    print("\n")
    print("-" * 80)
    print("COLUMN MISMATCH COUNTS")
    print("-" * 80)

    counts = results[
        "column_mismatch_counts"
    ]

    for position, count in counts.most_common():

        print(
            f"Position {position:<5} "
            f"{count:>12,}"
        )

    # --------------------------------------------------------
    # Difference signatures
    # --------------------------------------------------------

    print("\n")
    print("-" * 80)
    print("DIFFERENCE SIGNATURES")
    print("-" * 80)

    signatures = results[
        "difference_signature_counts"
    ]

    for signature, count in (
        signatures.most_common()
    ):

        signature_text = " + ".join(
            f"{position}:{legacy_name}"
            for (
                position,
                legacy_name,
                new_name
            )
            in signature
        )

        print(
            f"{signature_text:<65}"
            f"{count:>10,}"
        )


# ============================================================
# SAMPLE REPORT
# ============================================================

def print_samples(
    results,
    limit=5,
):

    print("\n")
    print("=" * 80)
    print("SAMPLE MISMATCHES")
    print("=" * 80)

    samples = results[
        "samples_by_column"
    ]

    for position in sorted(
        samples.keys()
    ):

        records = samples[
            position
        ]

        if not records:
            continue

        first = records[0]

        print("\n")
        print("-" * 80)

        print(
            f"POSITION {position} | "
            f"LEGACY: {first['legacy_column']} | "
            f"NEW: {first['new_column']}"
        )

        print("-" * 80)

        for sample in records[:limit]:

            print(
                f"\nLegacy row : "
                f"{sample['legacy_index']}"
            )

            print(
                f"New row    : "
                f"{sample['new_index']}"
            )

            print(
                f"Similarity : "
                f"{sample['similarity']:.2%}"
            )

            print(
                f"Legacy     : "
                f"{repr(sample['legacy_value'])}"
            )

            print(
                f"New        : "
                f"{repr(sample['new_value'])}"
            )


# ============================================================
# EXPORT
# ============================================================

def export_results(
    results,
    structure_result,
    output_directory,
):

    os.makedirs(
        output_directory,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Overall summary
    # --------------------------------------------------------

    pd.DataFrame(
        [
            results["summary"]
        ]
    ).to_csv(
        os.path.join(
            output_directory,
            "summary.csv",
        ),
        index=False,
    )

    # --------------------------------------------------------
    # Column mismatch counts
    # --------------------------------------------------------

    rows = []

    for position, count in (
        results[
            "column_mismatch_counts"
        ].items()
    ):

        rows.append(
            {
                "position":
                    position,

                "mismatch_count":
                    count,
            }
        )

    pd.DataFrame(rows).sort_values(
        "mismatch_count",
        ascending=False,
    ).to_csv(
        os.path.join(
            output_directory,
            "column_mismatch_counts.csv",
        ),
        index=False,
    )

    # --------------------------------------------------------
    # Difference signatures
    # --------------------------------------------------------

    rows = []

    for signature, count in (
        results[
            "difference_signature_counts"
        ].items()
    ):

        rows.append(
            {
                "difference_signature":
                    " | ".join(
                        f"{position}:{legacy_name}"
                        for (
                            position,
                            legacy_name,
                            new_name
                        )
                        in signature
                    ),

                "record_count":
                    count,
            }
        )

    pd.DataFrame(rows).sort_values(
        "record_count",
        ascending=False,
    ).to_csv(
        os.path.join(
            output_directory,
            "difference_signatures.csv",
        ),
        index=False,
    )

    # --------------------------------------------------------
    # Detailed mismatches
    # --------------------------------------------------------

    pd.DataFrame(
        results[
            "detailed_results"
        ]
    ).to_csv(
        os.path.join(
            output_directory,
            "detailed_mismatches.csv",
        ),
        index=False,
    )

    # --------------------------------------------------------
    # Samples
    # --------------------------------------------------------

    sample_rows = []

    for position, samples in (
        results[
            "samples_by_column"
        ].items()
    ):

        for sample in samples:

            sample_rows.append(
                {
                    "position":
                        position,

                    "legacy_column":
                        sample[
                            "legacy_column"
                        ],

                    "new_column":
                        sample[
                            "new_column"
                        ],

                    "legacy_row":
                        sample[
                            "legacy_index"
                        ],

                    "new_row":
                        sample[
                            "new_index"
                        ],

                    "similarity":
                        sample[
                            "similarity"
                        ],

                    "legacy_value":
                        sample[
                            "legacy_value"
                        ],

                    "new_value":
                        sample[
                            "new_value"
                        ],
                }
            )

    pd.DataFrame(
        sample_rows
    ).to_csv(
        os.path.join(
            output_directory,
            "mismatch_samples.csv",
        ),
        index=False,
    )

    # --------------------------------------------------------
    # Unresolved
    # --------------------------------------------------------

    pd.DataFrame(
        results[
            "unresolved_records"
        ]
    ).to_csv(
        os.path.join(
            output_directory,
            "unresolved_records.csv",
        ),
        index=False,
    )

    # --------------------------------------------------------
    # New-only
    # --------------------------------------------------------

    pd.DataFrame(
        {
            "new_index":
                results[
                    "new_only_records"
                ]
        }
    ).to_csv(
        os.path.join(
            output_directory,
            "new_only_records.csv",
        ),
        index=False,
    )

    # --------------------------------------------------------
    # Column name differences
    # --------------------------------------------------------

    pd.DataFrame(
        structure_result[
            "column_differences"
        ]
    ).to_csv(
        os.path.join(
            output_directory,
            "column_name_differences.csv",
        ),
        index=False,
    )

    print(
        f"\nOutput written to: "
        f"{output_directory}"
    )





# ============================================================
# NUMERIC COLUMN IDENTIFICATION
# ============================================================

def _try_decimal(value: str) -> bool:
    """
    Return True if value is a valid finite decimal number.

    Handles all of these forms without any pre-processing:
        1000          1000.00       1000.0000
        .04           0.04          -0.04
        +1.5          1e3
        (leading/trailing whitespace is stripped first)

    Uses Decimal rather than float so the same parser is used
    for both detection and rollup computation — no surprises
    when a value passes detection but later fails summation.
    """
    from decimal import Decimal, InvalidOperation
    import math
    try:
        d = Decimal(str(value).strip())
        return d.is_finite()
    except (InvalidOperation, TypeError, ValueError):
        # Fall back to float for scientific notation (1e3 etc.)
        try:
            f = float(str(value).strip())
            return math.isfinite(f)
        except (ValueError, TypeError):
            return False


# Minimum fraction of non-empty values that must parse as
# numeric for the column to be treated as a numeric column.
_NUMERIC_THRESHOLD = 0.80


def identify_numeric_columns(
    df: pd.DataFrame,
    null_token: str,
) -> list:
    """
    A column is treated as numeric when AT LEAST 80% of its
    non-empty, non-null values parse as a finite decimal number.

    This handles real-world columns that are mostly numeric but
    contain the occasional string sentinel (e.g. "N/A", "TBD").
    Non-parseable values are skipped silently during rollup.

    Mixed precision is fully supported across rows in the same
    column — e.g. 1000, 1000.00, .04, 0.0400 are all valid.

    ID-looking columns are excluded even if they exceed the
    threshold, since summing row identifiers is not meaningful.

    Returns a list of column names.
    Attaches parse rates to identify_numeric_columns._parse_rates
    so callers can surface them in reports.
    """

    numeric_cols = []
    _parse_rates = {}

    for col in df.columns:

        non_empty = [
            v for v in df[col]
            if v not in ("", null_token)
            and not (
                isinstance(v, float)
                and pd.isna(v)
            )
        ]

        if not non_empty:
            continue

        parsed_count = sum(1 for v in non_empty if _try_decimal(v))
        parse_rate   = parsed_count / len(non_empty)

        if parse_rate < _NUMERIC_THRESHOLD:
            continue

        # Exclude columns that look like identifiers.
        if _looks_like_id_col(col, non_empty):
            continue

        numeric_cols.append(col)
        _parse_rates[col] = parse_rate

    # Attach parse rates as a function attribute so compute_rollups
    # can include them without changing signatures.
    identify_numeric_columns._parse_rates = _parse_rates

    return numeric_cols


# ============================================================
# GROUPING COLUMN IDENTIFICATION
# ============================================================

import re as _re

# Patterns that suggest a column is an ID or date — these are
# excluded from grouping even if they are low-cardinality.
_ID_PATTERNS   = _re.compile(
    r"(^|_)(id|key|code|num|no|number|ref|uuid|guid|seq)($|_)",
    _re.IGNORECASE,
)
_DATE_PATTERNS = _re.compile(
    r"(date|time|ts|timestamp|dt|year|month|day)",
    _re.IGNORECASE,
)
# Pattern that detects whether a value *looks* like a date.
_DATE_VALUE_RE = _re.compile(
    r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$"
)


def _looks_like_id_col(col: str, sample_values: list) -> bool:
    """Heuristic: column name or values look like identifiers."""
    if _ID_PATTERNS.search(col):
        return True
    # If most values are unique integers they are likely IDs.
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
        1 for v in sample_values[:20]
        if _DATE_VALUE_RE.match(str(v))
    )
    return date_hits > len(sample_values[:20]) * 0.5


def identify_grouping_columns(
    df: pd.DataFrame,
    numeric_cols: list,
    config: "ReconciliationConfig",
) -> list:
    """
    Identify low-cardinality text columns suitable for grouping.

    A column qualifies when ALL of these hold:
      1. It is not a numeric column.
      2. Its distinct non-empty value count is <=
         config.max_grouping_cardinality.
      3. Its name and sample values do not look like an ID.
      4. Its name and sample values do not look like a date.
    """

    numeric_set = set(numeric_cols)
    grouping    = []

    for col in df.columns:

        if col in numeric_set:
            continue

        non_empty = [
            v for v in df[col]
            if v not in ("", config.null_token)
        ]

        if not non_empty:
            continue

        distinct = len(set(non_empty))

        if distinct > config.max_grouping_cardinality:
            continue

        if _looks_like_id_col(col, non_empty):
            continue

        if _looks_like_date_col(col, non_empty):
            continue

        grouping.append(col)

    return grouping


# ============================================================
# ROLLUP COMPUTATION
# ============================================================

from decimal import Decimal, InvalidOperation


def _safe_decimal(value: str):
    """Parse a string to Decimal; return None on failure."""
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
    error_legacy_indices=None,
    error_new_indices=None,
) -> dict:
    """
    Compute per-file and grouped rollups for all numeric columns.

    Uses Decimal arithmetic throughout to avoid float precision
    drift in summation — important for financial reconciliation.

    Non-parseable values in a predominantly-numeric column are
    skipped during summation and counted separately so the report
    can surface how many rows were excluded per column.

    Mixed decimal precision (1000 / 1000.00 / .04 / 0.0400) is
    handled transparently — Decimal preserves each value exactly
    as written, so no rounding or normalisation occurs.

    Returns:
    {
      "numeric_columns": [...],
      "grouping_columns": [...],
      "parse_rates": {col: float},   # fraction that parsed
      "file_totals": {
          col: {
              "legacy_sum": Decimal, "new_sum": Decimal,
              "legacy_count": int,   "new_count": int,
              "legacy_skipped": int, "new_skipped": int,
              "legacy_avg": Decimal, "new_avg": Decimal,
              "sum_match": bool,     "avg_match": bool,
          }
      },
      "grouped_totals": {
          group_col: {
              group_value: {
                  numeric_col: {same keys as file_totals entry}
              }
          }
      },
      "file_totals_without_errors": {...},
      "grouped_totals_without_errors": {...}
    }
    """

    def _col_decimals(df, col):
        """
        Return (parsed_list, skipped_count).

        parsed_list : [(index, Decimal)] for values that parsed.
        skipped_count : number of non-empty values that did not parse.
        """
        parsed  = []
        skipped = 0
        for idx, v in df[col].items():
            if v in ("", null_token):
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
            new_vals,    n_skip = _col_decimals(new_frame,    col)

            l_sum   = sum(d for _, d in legacy_vals)
            n_sum   = sum(d for _, d in new_vals)
            l_count = len(legacy_vals)
            n_count = len(new_vals)
            l_avg   = (l_sum / l_count) if l_count else Decimal(0)
            n_avg   = (n_sum / n_count) if n_count else Decimal(0)

            totals[col] = {
                "legacy_sum":     l_sum,
                "new_sum":        n_sum,
                "legacy_count":   l_count,
                "new_count":      n_count,
                "legacy_skipped": l_skip,
                "new_skipped":    n_skip,
                "legacy_avg":     l_avg,
                "new_avg":        n_avg,
                "sum_match":      l_sum == n_sum,
                "avg_match":      l_avg == n_avg,
            }
        return totals

    def _summarise_grouped_frame(legacy_frame, new_frame):
        grouped_totals = {}

        for g_col in grouping_cols:
            grouped_totals[g_col] = {}

            all_vals = set(legacy_frame[g_col].tolist()) | set(new_frame[g_col].tolist())
            all_vals.discard("")
            all_vals.discard(null_token)

            for g_val in sorted(all_vals):
                legacy_mask = legacy_frame[g_col] == g_val
                new_mask    = new_frame[g_col]    == g_val

                legacy_sub = legacy_frame[legacy_mask]
                new_sub    = new_frame[new_mask]

                col_stats = {}
                for n_col in numeric_cols:
                    lv, l_skip = _col_decimals(legacy_sub, n_col)
                    nv, n_skip = _col_decimals(new_sub,    n_col)

                    l_sum   = sum(d for _, d in lv)
                    n_sum   = sum(d for _, d in nv)
                    l_count = len(lv)
                    n_count = len(nv)
                    l_avg   = (l_sum / l_count) if l_count else Decimal(0)
                    n_avg   = (n_sum / n_count) if n_count else Decimal(0)

                    col_stats[n_col] = {
                        "legacy_sum":     l_sum,
                        "new_sum":        n_sum,
                        "legacy_count":   l_count,
                        "new_count":      n_count,
                        "legacy_skipped": l_skip,
                        "new_skipped":    n_skip,
                        "legacy_avg":     l_avg,
                        "new_avg":        n_avg,
                        "sum_match":      l_sum == n_sum,
                        "avg_match":      l_avg == n_avg,
                    }

                grouped_totals[g_col][g_val] = col_stats

        return grouped_totals

    # ── File-level totals ────────────────────────────────────

    # Pick up parse rates stored by identify_numeric_columns.
    parse_rates = getattr(
        identify_numeric_columns,
        "_parse_rates",
        {},
    )

    file_totals = _summarise_numeric_frame(legacy_df, new_df)
    grouped_totals = _summarise_grouped_frame(legacy_df, new_df)

    error_legacy_indices = set(error_legacy_indices or [])
    error_new_indices = set(error_new_indices or [])

    legacy_without_errors = legacy_df.loc[~legacy_df.index.isin(error_legacy_indices)] if error_legacy_indices else legacy_df
    new_without_errors = new_df.loc[~new_df.index.isin(error_new_indices)] if error_new_indices else new_df

    file_totals_without_errors = _summarise_numeric_frame(legacy_without_errors, new_without_errors)
    grouped_totals_without_errors = _summarise_grouped_frame(legacy_without_errors, new_without_errors)

    return {
        "numeric_columns":  numeric_cols,
        "grouping_columns": grouping_cols,
        "parse_rates":      parse_rates,
        "file_totals":      file_totals,
        "grouped_totals":   grouped_totals,
        "file_totals_without_errors": file_totals_without_errors,
        "grouped_totals_without_errors": grouped_totals_without_errors,
    }

# ============================================================
# UNIFIED SINGLE-FILE CSV REPORT
# ============================================================

def export_unified_csv(
    results,
    structure_result,
    config: "ReconciliationConfig",
    legacy_df: "pd.DataFrame",
    new_df: "pd.DataFrame",
    legacy_eol,
    new_eol,
    rollup=None,
):
    """
    Write one consolidated CSV report with clearly labelled
    sections in this order:

        1. ENCODING
        2. SCHEMA MISMATCHES
        3. COUNT MISMATCH
        4. NEWLINE / EOL
        5. MISMATCHED DATA  (one block per error type)
    """

    import csv as _csv

    os.makedirs(
        config.output_directory,
        exist_ok=True,
    )

    out_path = os.path.join(
        config.output_directory,
        "reconciliation_report.csv",
    )

    summary     = results["summary"]
    col_diffs   = structure_result["column_differences"]
    legacy_cols = list(legacy_df.columns)
    new_cols    = list(new_df.columns)

    samples_by_error_type  = results["samples_by_error_type"]
    column_error_type_counts = results["column_error_type_counts"]

    rows = []   # list of lists – written as CSV at the end

    # Keep visual separation consistent in the CSV report.
    # A "blank line" means an empty CSV record.  Use two empty
    # records so every major section, error set, and newly added
    # subsection is separated by at least two visible blank lines.
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

    # ─────────────────────────────────────────────────────────
    # 1. ENCODING
    # ─────────────────────────────────────────────────────────

    section("ENCODING")

    rows.append(["field", "value"])
    rows.append(["legacy_file",    config.legacy_file])
    rows.append(["legacy_encoding", summary["legacy_encoding"]])
    rows.append(["new_file",       config.new_file])
    rows.append(["new_encoding",   summary["new_encoding"]])

    if summary["encoding_mismatch"]:
        summary_line(
            f"ENCODING MISMATCH: legacy={summary['legacy_encoding']} "
            f"vs new={summary['new_encoding']}. "
            "Comparison is valid — both files decoded to Unicode "
            "before comparison — but verify this is intentional."
        )
    else:
        summary_line("Encodings match — no encoding difference detected.")

    # ─────────────────────────────────────────────────────────
    # 2. SCHEMA MISMATCHES
    # ─────────────────────────────────────────────────────────

    section("SCHEMA MISMATCHES (COLUMN NAME DIFFERENCES)")

    if not col_diffs:
        summary_line(
            "No schema differences — all column names match "
            "at every position."
        )
    else:
        legacy_name_list = ", ".join(
            d["legacy_column"] for d in col_diffs
        )
        new_name_list = ", ".join(
            d["new_column"] for d in col_diffs
        )
        summary_line(
            f"{len(col_diffs)} column name difference(s) found. "
            f"Legacy: [{legacy_name_list}] | "
            f"New: [{new_name_list}]"
        )
        rows.append(["position", "legacy_column_name", "new_column_name"])
        for d in col_diffs:
            rows.append([
                d["position"],
                d["legacy_column"] or "",
                d["new_column"]    or "",
            ])

    # ─────────────────────────────────────────────────────────
    # 3. COUNT MISMATCH
    # ─────────────────────────────────────────────────────────

    section("COUNT MISMATCH")

    legacy_count = summary["legacy_records"]
    new_count    = summary["new_records"]
    diff         = legacy_count - new_count

    if diff == 0:
        summary_line(
            f"Row counts match — both files have {legacy_count:,} records."
        )
    else:
        summary_line(
            f"Row count difference of {abs(diff):,}: "
            f"legacy={legacy_count:,}, new={new_count:,}. "
            + (
                f"{abs(diff):,} record(s) present in legacy but missing in new."
                if diff > 0 else
                f"{abs(diff):,} record(s) present in new but missing in legacy."
            )
        )

    rows.append(["metric",           "legacy",       "new"])
    rows.append(["total_records",     legacy_count,   new_count])
    rows.append(["exact_matches",
                 summary["exact_raw_matches"],
                 summary["exact_raw_matches"]])
    rows.append(["mismatched_records",
                 summary["mismatched_records"], ""])
    rows.append(["unresolved_legacy",
                 summary["unresolved_legacy_records"], ""])
    rows.append(["new_only_records",  "", summary["new_only_records"]])

    # ─────────────────────────────────────────────────────────
    # 4. NEWLINE / EOL
    # ─────────────────────────────────────────────────────────

    section("NEWLINE / EOL DIFFERENCES")

    legacy_file_eol = legacy_eol["file_eol"]
    new_file_eol    = new_eol["file_eol"]
    eol_mismatch    = legacy_file_eol != new_file_eol
    legacy_embedded = legacy_eol["embedded_newline_rows"]
    new_embedded    = new_eol["embedded_newline_rows"]

    # -- file-level EOL
    if eol_mismatch:
        summary_line(
            f"FILE-LEVEL EOL MISMATCH: "
            f"legacy uses {legacy_file_eol}, new uses {new_file_eol}."
        )
    else:
        summary_line(
            f"File-level line endings match ({legacy_file_eol})."
        )

    rows.append(["source", "file_eol", "CRLF_count", "LF_count", "CR_count"])
    for label, eol in [("legacy", legacy_eol), ("new", new_eol)]:
        rows.append([
            label,
            eol["file_eol"],
            eol["eol_counts"]["CRLF"],
            eol["eol_counts"]["LF"],
            eol["eol_counts"]["CR"],
        ])

    # -- embedded newlines
    total_embedded = len(legacy_embedded) + len(new_embedded)
    if total_embedded == 0:
        summary_line(
            "No embedded newlines found inside any field values."
        )
    else:
        col_set = set(
            r["col"] for r in legacy_embedded + new_embedded
        )
        summary_line(
            f"{total_embedded} field value(s) contain embedded newlines "
            f"across column(s): {', '.join(sorted(col_set))}."
        )

        blank()
        rows.append(["--- Embedded newlines in LEGACY file ---"])
        if legacy_embedded:
            rows.append(["data_row", "column", "raw_value"])
            for r in legacy_embedded:
                rows.append([r["row"], r["col"], r["value"]])
        else:
            rows.append(["(none)"])

        blank()
        rows.append(["--- Embedded newlines in NEW file ---"])
        if new_embedded:
            rows.append(["data_row", "column", "raw_value"])
            for r in new_embedded:
                rows.append([r["row"], r["col"], r["value"]])
        else:
            rows.append(["(none)"])

    # ─────────────────────────────────────────────────────────
    # 5. MISMATCHED DATA
    # One block per (column, error-type).
    # Error type = distinct (legacy_value, new_value) pair.
    # Each block: column header row, then 1 legacy row,
    # then 1 new row (full rows, all column values).
    # ─────────────────────────────────────────────────────────

    section("MISMATCHED DATA")

    if not samples_by_error_type:
        summary_line("No data mismatches found.")
    else:
        # Group error types by column position so all blocks
        # for the same column are consecutive.
        by_position = defaultdict(list)
        for key in samples_by_error_type:
            position, legacy_val, new_val = key
            by_position[position].append(key)

        for position in sorted(by_position.keys()):

            keys_for_col = sorted(
                by_position[position],
                key=lambda k: (k[1], k[2]),
            )

            sample_0 = samples_by_error_type[keys_for_col[0]]
            col_legacy_name = sample_0["legacy_column"]
            col_new_name    = sample_0["new_column"]

            col_label = (
                col_legacy_name
                if col_legacy_name == col_new_name
                else f"{col_legacy_name} / {col_new_name}"
            )

            type_count    = len(keys_for_col)
            total_records = sum(
                column_error_type_counts[k]
                for k in keys_for_col
            )

            summary_line(
                f"Column '{col_label}' (position {position}): "
                f"{type_count} distinct error type(s) across "
                f"{total_records:,} mismatched record(s)."
            )

            for key in keys_for_col:

                position, legacy_val, new_val = key
                sample = samples_by_error_type[key]
                count  = column_error_type_counts[key]

                blank()
                # Column name as block header
                rows.append([col_label])
                # Error type summary
                rows.append([
                    f"error_type: legacy='{legacy_val}' "
                    f"vs new='{new_val}' "
                    f"| occurrences={count:,}"
                ])

                # Full header row (all column names)
                rows.append(legacy_cols)

                # Legacy full row
                legacy_full = sample["legacy_full_row"]
                rows.append(
                    ["[LEGACY]"] + legacy_full[1:]
                    if len(legacy_full) > 1
                    else ["[LEGACY]"] + legacy_full
                )
                # Prepend the source label cleanly
                legacy_row_out = list(legacy_full)
                new_row_out    = list(sample["new_full_row"])

                # Replace first value with source label
                rows[-1] = (
                    ["[LEGACY]"]
                    + legacy_row_out
                )
                rows.append(
                    ["[NEW]"]
                    + new_row_out
                )

    # ─────────────────────────────────────────────────────────
    # 6. NUMERIC ROLLUP VALIDATION
    # ─────────────────────────────────────────────────────────

    rollup = rollup or results.get("rollup")

    section("NUMERIC ROLLUP VALIDATION")

    if not rollup or not rollup["numeric_columns"]:

        summary_line(
            "No numeric columns identified — rollup skipped."
        )

    else:

        num_cols    = rollup["numeric_columns"]
        grp_cols    = rollup["grouping_columns"]
        ft          = rollup["file_totals"]
        parse_rates = rollup.get("parse_rates", {})

        # Build summary with parse rate per column so the reader
        # knows how many values were actually numeric.
        col_summaries = []
        for col in num_cols:
            rate = parse_rates.get(col, 1.0)
            col_summaries.append(
                f"{col} ({rate:.0%} numeric)"
            )

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
            summary_line(
                "No low-cardinality grouping columns identified."
            )

        def write_numeric_rollup_section(title, totals, grouped_totals, include_grouped_values=True):
            blank()
            rows.append([f"--- {title} ---"])
            blank()

            header = ["metric"]
            for col in num_cols:
                header += [f"{col} [LEGACY]", f"{col} [NEW]"]
            rows.append(header)

            sum_row = ["SUM"]
            for col in num_cols:
                s = totals[col]
                match_flag = "" if s["sum_match"] else " !"
                sum_row += [
                    str(s["legacy_sum"]) + match_flag,
                    str(s["new_sum"]) + match_flag,
                ]
            rows.append(sum_row)

            avg_row = ["AVERAGE"]
            for col in num_cols:
                s = totals[col]
                match_flag = "" if s["avg_match"] else " !"
                avg_row += [
                    str(round(s["legacy_avg"], 6)) + match_flag,
                    str(round(s["new_avg"], 6)) + match_flag,
                ]
            rows.append(avg_row)

            cnt_row = ["COUNT (parsed)"]
            for col in num_cols:
                s = totals[col]
                cnt_row += [s["legacy_count"], s["new_count"]]
            rows.append(cnt_row)

            any_skipped = any(
                totals[col]["legacy_skipped"] > 0
                or totals[col]["new_skipped"] > 0
                for col in num_cols
            )
            if any_skipped:
                skip_row = ["COUNT (skipped — non-numeric values)"]
                for col in num_cols:
                    s = totals[col]
                    skip_row += [s["legacy_skipped"], s["new_skipped"]]
                rows.append(skip_row)

            mismatched_cols = [
                col for col in num_cols
                if not totals[col]["sum_match"] or not totals[col]["avg_match"]
            ]
            if mismatched_cols:
                summary_line(
                    f"ROLLUP MISMATCH on {len(mismatched_cols)} column(s): "
                    + ", ".join(mismatched_cols)
                    + ". Cells marked with ' !' indicate a difference."
                )
            else:
                summary_line(
                    "All numeric column totals and averages match "
                    "between legacy and new files."
                )

            if include_grouped_values and grp_cols:
                blank()
                rows.append([f"--- GROUPED ROLLUPS ({title}) ---"])

                for g_col in grp_cols:
                    blank()
                    rows.append([f"Grouped by: {g_col}"])
                    blank()

                    g_header = [g_col]
                    for col in num_cols:
                        g_header += [
                            f"{col} SUM [LEGACY]",
                            f"{col} SUM [NEW]",
                            f"{col} AVG [LEGACY]",
                            f"{col} AVG [NEW]",
                        ]
                    rows.append(g_header)

                    grp_data = grouped_totals.get(g_col, {})
                    for g_val in sorted(grp_data.keys()):
                        col_stats = grp_data[g_val]
                        data_row = [g_val]

                        for col in num_cols:
                            cs = col_stats.get(col, {})
                            l_sum = cs.get("legacy_sum", "")
                            n_sum = cs.get("new_sum", "")
                            l_avg = cs.get("legacy_avg", "")
                            n_avg = cs.get("new_avg", "")

                            sum_flag = "" if cs.get("sum_match", True) else " !"
                            avg_flag = "" if cs.get("avg_match", True) else " !"

                            data_row += [
                                str(l_sum) + sum_flag,
                                str(n_sum) + sum_flag,
                                str(round(l_avg, 6)) + avg_flag
                                if isinstance(l_avg, object) and hasattr(l_avg, "__round__")
                                else str(l_avg) + avg_flag,
                                str(round(n_avg, 6)) + avg_flag
                                if isinstance(n_avg, object) and hasattr(n_avg, "__round__")
                                else str(n_avg) + avg_flag,
                            ]

                        rows.append(data_row)

        file_totals_with_errors = ft
        file_totals_without_errors = rollup.get("file_totals_without_errors", ft)
        grouped_totals_with_errors = rollup["grouped_totals"]
        grouped_totals_without_errors = rollup.get("grouped_totals_without_errors", grouped_totals_with_errors)

        write_numeric_rollup_section(
            "FILE-LEVEL ROLLUP (WITH ERRORS)",
            file_totals_with_errors,
            grouped_totals_with_errors,
            include_grouped_values=bool(grp_cols),
        )

        write_numeric_rollup_section(
            "FILE-LEVEL ROLLUP (WITHOUT ERRORS)",
            file_totals_without_errors,
            grouped_totals_without_errors,
            include_grouped_values=bool(grp_cols),
        )

    # ─────────────────────────────────────────────────────────
    # Write
    # ─────────────────────────────────────────────────────────

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.writer(f, quoting=_csv.QUOTE_ALL)
        for row in rows:
            writer.writerow([str(c) for c in row])

    print(f"\nUnified report written to: {out_path}")
    return out_path

# ============================================================
# MAIN
# ============================================================

def parse_args():

    """
    Parse command-line arguments.

    Required positional arguments:

        legacy_file   new_file

    Optional encoding flags (omit to auto-detect):

        --legacy-encoding ENCODING
        --new-encoding    ENCODING

    Usage:

        python recon.py legacy.csv current.csv
        python recon.py legacy.csv current.csv --legacy-encoding utf-8 --new-encoding utf-16

    Other optional flags:

        --delimiter CHAR        field delimiter     (default: ,)
        --output    DIR         output directory    (default: reconciliation_output)
        --min-similarity FLOAT  (default: 0.80)
        --ambiguity-margin FLOAT (default: 0.05)
        --max-block-columns INT (default: 3)
        --max-candidates INT    (default: 100)
        --sample-limit INT      (default: 5)
        --quotechar CHAR        (default: ")
        --no-doublequote        disable doublequote handling
    """

    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Reconcile a legacy CSV against a new CSV.\n\n"
            "  python recon.py legacy.csv current.csv\n"
            "  python recon.py legacy.csv current.csv "
            "--legacy-encoding utf-8 --new-encoding utf-16"
        ),
        formatter_class=(
            argparse.RawDescriptionHelpFormatter
        ),
    )

    # --------------------------------------------------------
    # Required positional arguments
    # --------------------------------------------------------

    parser.add_argument(
        "legacy_file",
        help="Path to the legacy CSV file",
    )

    parser.add_argument(
        "new_file",
        help="Path to the new/current CSV file",
    )

    # --------------------------------------------------------
    # Optional encoding flags
    # Omit both to let chardet auto-detect each file.
    # --------------------------------------------------------

    parser.add_argument(
        "--legacy-encoding",
        default="",
        dest="legacy_encoding",
        help=(
            "Encoding of the legacy file "
            "(e.g. utf-8, utf-16, latin-1). "
            "Auto-detected when omitted."
        ),
    )

    parser.add_argument(
        "--new-encoding",
        default="",
        dest="new_encoding",
        help=(
            "Encoding of the new file "
            "(e.g. utf-8, utf-16, latin-1). "
            "Auto-detected when omitted."
        ),
    )

    # --------------------------------------------------------
    # Optional flags
    # --------------------------------------------------------

    parser.add_argument(
        "--delimiter",
        default=",",
        help="Field delimiter character (default: ',')",
    )

    parser.add_argument(
        "--output",
        default="reconciliation_output",
        dest="output_directory",
        help="Output directory (default: reconciliation_output)",
    )

    parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.80,
        dest="min_similarity",
        help="Minimum similarity threshold (default: 0.80)",
    )

    parser.add_argument(
        "--ambiguity-margin",
        type=float,
        default=0.05,
        dest="ambiguity_margin",
        help=(
            "Minimum gap between best and second-best "
            "candidate (default: 0.05)"
        ),
    )

    parser.add_argument(
        "--max-block-columns",
        type=int,
        default=3,
        dest="max_block_columns",
        help="Max columns in a blocking key (default: 3)",
    )

    parser.add_argument(
        "--max-candidates",
        type=int,
        default=100,
        dest="max_candidates_per_row",
        help="Max candidates evaluated per row (default: 100)",
    )

    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        dest="sample_limit",
        help="Max mismatch samples stored per column (default: 5)",
    )

    parser.add_argument(
        "--quotechar",
        default='"',
        help="Quote character wrapping field values (default: \")",
    )

    parser.add_argument(
        "--no-doublequote",
        action="store_false",
        dest="doublequote",
        help=(
            "Disable doublequote handling "
            "(by default, \"\" inside a quoted field "
            "is treated as a literal \")"
        ),
    )

    parser.add_argument(
        "--max-grouping-cardinality",
        type=int,
        default=20,
        dest="max_grouping_cardinality",
        help=(
            "Maximum distinct values a text column may have "
            "to qualify as a grouping column in the rollup "
            "section (default: 20)"
        ),
    )

    parser.set_defaults(doublequote=True)

    return parser.parse_args()


if __name__ == "__main__":

    args = parse_args()

    config = ReconciliationConfig(

        legacy_file=args.legacy_file,
        legacy_encoding=args.legacy_encoding,

        new_file=args.new_file,
        new_encoding=args.new_encoding,

        delimiter=args.delimiter,

        # Candidate matching
        min_similarity=args.min_similarity,
        ambiguity_margin=args.ambiguity_margin,
        max_block_columns=args.max_block_columns,
        max_candidates_per_row=args.max_candidates_per_row,

        # Reporting
        sample_limit=args.sample_limit,
        output_directory=args.output_directory,

        # Quote handling
        quotechar=args.quotechar,
        doublequote=args.doublequote,

        # Rollup
        max_grouping_cardinality=args.max_grouping_cardinality,
    )

    # --------------------------------------------------------
    # 1. LOAD CSVs
    # --------------------------------------------------------

    print(
        "\nLoading CSV files..."
    )

    legacy_df, new_df = load_csv(
        config
    )

    print(
        f"Legacy DF shape : "
        f"{legacy_df.shape}"
    )

    print(
        f"New DF shape    : "
        f"{new_df.shape}"
    )

    # --------------------------------------------------------
    # 2. STRUCTURAL VALIDATION
    # --------------------------------------------------------

    structure_result = (
        validate_structure(
            legacy_df,
            new_df,
        )
    )

    if not structure_result[
        "column_count_match"
    ]:

        raise ValueError(
            "\nColumn count mismatch.\n"
            f"Legacy columns = "
            f"{structure_result['legacy_column_count']}\n"
            f"New columns = "
            f"{structure_result['new_column_count']}\n\n"
            "Column position is being used as the "
            "comparison basis, therefore the files "
            "must have the same number of columns."
        )

    # --------------------------------------------------------
    # 3. RECONCILE
    # --------------------------------------------------------

    print(
        "\nStarting reconciliation..."
    )

    results = reconcile(
        legacy_df,
        new_df,
        config,
    )

    # --------------------------------------------------------
    # 4. EOL DETECTION
    # --------------------------------------------------------

    print("\nDetecting line endings...")

    legacy_eol = detect_eol(
        config.legacy_file,
        config.legacy_encoding,
    )

    new_eol = detect_eol(
        config.new_file,
        config.new_encoding,
    )

    print(
        f"  legacy EOL : {legacy_eol['file_eol']} "
        f"(CRLF={legacy_eol['eol_counts']['CRLF']}, "
        f"LF={legacy_eol['eol_counts']['LF']}, "
        f"CR={legacy_eol['eol_counts']['CR']})"
    )

    print(
        f"  new    EOL : {new_eol['file_eol']} "
        f"(CRLF={new_eol['eol_counts']['CRLF']}, "
        f"LF={new_eol['eol_counts']['LF']}, "
        f"CR={new_eol['eol_counts']['CR']})"
    )

    # --------------------------------------------------------
    # 5. PRINT REPORT  (console summary — unchanged)
    # --------------------------------------------------------

    print_report(
        results,
        structure_result,
    )

    print_samples(
        results,
        limit=config.sample_limit,
    )

    # --------------------------------------------------------
    # 6. NUMERIC ROLLUP COMPUTATION
    # --------------------------------------------------------

    print("\nIdentifying numeric and grouping columns...")

    numeric_cols = identify_numeric_columns(
        legacy_df,
        config.null_token,
    )

    grouping_cols = identify_grouping_columns(
        legacy_df,
        numeric_cols,
        config,
    )

    print(
        f"  Numeric columns  : "
        + (", ".join(numeric_cols) if numeric_cols else "(none)")
    )

    print(
        f"  Grouping columns : "
        + (", ".join(grouping_cols) if grouping_cols else "(none)")
    )

    error_legacy_indices = [
        pair["legacy_index"]
        for pair in results["detailed_results"]
    ]
    error_new_indices = [
        pair["new_index"]
        for pair in results["detailed_results"]
    ]

    rollup = compute_rollups(
        legacy_df,
        new_df,
        numeric_cols,
        grouping_cols,
        config.null_token,
        error_legacy_indices=error_legacy_indices,
        error_new_indices=error_new_indices,
    )

    # --------------------------------------------------------
    # 7. UNIFIED SINGLE-FILE EXPORT
    # --------------------------------------------------------

    out_path = export_unified_csv(
        results,
        structure_result,
        config,
        legacy_df,
        new_df,
        legacy_eol,
        new_eol,
        rollup=rollup,
    )

    print(
        "\nReconciliation completed."
    )
