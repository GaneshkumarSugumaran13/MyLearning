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
        # ----------------------------------------------------

        for difference in differences:

            position = (
                difference["position"]
            )

            column = (
                difference["legacy_column"]
            )

            column_mismatch_counts[
                position
            ] += 1

            if len(
                samples_by_column[position]
            ) < config.sample_limit:

                samples_by_column[
                    position
                ].append(
                    {
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
                    }
                )

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
# MAIN
# ============================================================

def parse_args():

    """
    Parse command-line arguments.

    Required positional arguments:

        legacy_file   legacy_encoding   new_file   new_encoding

    Usage:

        python recon.py legacy.csv utf-8 current.csv utf-16

    Optional flags (must follow the four positional args):

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
            "  python recon.py "
            "legacy.csv utf-8 "
            "current.csv utf-16"
        ),
        formatter_class=(
            argparse.RawDescriptionHelpFormatter
        ),
    )

    # --------------------------------------------------------
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
    # Optional encoding arguments
    # --------------------------------------------------------
    
    parser.add_argument(
        "--legacy-encoding",
        default="",
        dest="legacy_encoding",
        help=(
            "Encoding of the legacy file "
            "(e.g. utf-8, utf-16, latin-1). "
            "If omitted, encoding is auto-detected."
        ),
    )
    
    parser.add_argument(
        "--new-encoding",
        default="",
        dest="new_encoding",
        help=(
            "Encoding of the new file "
            "(e.g. utf-8, utf-16, latin-1). "
            "If omitted, encoding is auto-detected."
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
    # 4. PRINT REPORT
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
    # 5. EXPORT
    # --------------------------------------------------------

    export_results(
        results,
        structure_result,
        config.output_directory,
    )

    print(
        "\nReconciliation completed."
    )
