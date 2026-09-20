# CSV Reconciliation Script — Reference Guide

## Overview

This script compares two CSV files — one exported from a **legacy system** and one from a **current/new system** — to identify exact matches, value-level mismatches, unmatched records, and records that exist only in the new file. It uses a two-pass matching strategy (exact fingerprint matching, then candidate-based fuzzy matching via blocking) and produces a set of CSV reports for analysis.

---

## Usage

The script is invoked from the command line. The four positional arguments are required; all other flags are optional.

```bash
python recon.py <legacy_file> <legacy_encoding> <new_file> <new_encoding> [options]
```

**Examples:**

```bash
# Both files UTF-8
python recon.py legacy.csv utf-8 current.csv utf-8

# Different encodings
python recon.py legacy.csv utf-8 current.csv utf-16

# With optional overrides
python recon.py legacy.csv latin-1 current.csv utf-8 \
    --delimiter ";" \
    --output my_results \
    --min-similarity 0.85 \
    --quotechar "'"
```

**All flags:**

| Flag | Default | Description |
|---|---|---|
| `--delimiter CHAR` | `,` | Field delimiter character |
| `--output DIR` | `reconciliation_output` | Output directory |
| `--min-similarity FLOAT` | `0.80` | Minimum similarity threshold for candidate acceptance |
| `--ambiguity-margin FLOAT` | `0.05` | Minimum gap between best and second-best candidate |
| `--max-block-columns INT` | `3` | Max columns combined in a blocking key |
| `--max-candidates INT` | `100` | Max candidates evaluated per legacy row |
| `--sample-limit INT` | `5` | Max mismatch samples stored per column |
| `--quotechar CHAR` | `"` | Quote character wrapping field values |
| `--no-doublequote` | *(off)* | Disable `""` → `"` handling inside quoted fields |

---

## Configuration Variables (`ReconciliationConfig`)

All runtime behaviour is governed by a single dataclass. When calling the script from the command line, these are populated automatically from the parsed arguments. When importing the module programmatically, construct the dataclass directly.

### File Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `legacy_file` | `str` | *(required)* | Path to the legacy system CSV file |
| `new_file` | `str` | *(required)* | Path to the current/new system CSV file |
| `delimiter` | `str` | `","` | CSV field delimiter |
| `encoding` | `str` | `"utf-8"` | Shared fallback encoding used when neither per-file encoding is set |
| `legacy_encoding` | `str` | `""` | Encoding for the legacy file. Takes precedence over `encoding` when set. |
| `new_encoding` | `str` | `""` | Encoding for the new file. Takes precedence over `encoding` when set. |

**Encoding resolution:** `load_csv` resolves each file's encoding as `legacy_encoding or encoding` (and likewise for new). This means:
- Pass both per-file encodings via CLI args → each file uses its own encoding.
- Leave both blank and set `encoding` only → both files use the shared value.
- Mix: set `legacy_encoding` only → legacy uses it; new falls back to `encoding`.

> **Cross-encoding comparison is safe.** `pd.read_csv` decodes each file from bytes to Python Unicode strings before any comparison takes place. Once both DataFrames are in memory, the encoding used to read them is irrelevant — all values are plain `str`. The only risk is passing the *wrong* encoding for a file, which would produce garbled values during decode. The `encoding_mismatch` flag in the summary is surfaced so reviewers can verify the difference was intentional.

### Quote Handling Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `quotechar` | `str` | `'"'` | Character used to wrap field values and column headers in the CSV. Enclosing quotes are stripped automatically; the inner content is preserved exactly. |
| `doublequote` | `bool` | `True` | When `True`, a pair of quote characters inside a quoted field (`""`) is interpreted as a single literal quote character. When `False`, the quote character has no special meaning inside a field. |

Both parameters apply equally to column headers and data values. There is no separate handling needed for quoted headers — pandas uses the same `quotechar` when parsing the header row.

### Candidate Matching Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `min_similarity` | `float` | `0.80` | Minimum fraction of columns that must match (normalized) for a candidate to be considered. Range: 0.0–1.0. Lower values allow weaker matches; higher values are stricter. |
| `ambiguity_margin` | `float` | `0.05` | The best candidate must beat the second-best by at least this margin. If the gap is smaller, the match is marked `AMBIGUOUS` and excluded. Prevents wrong pairings when two new rows look equally similar. |
| `max_block_columns` | `int` | `3` | Maximum number of columns that can be combined into a single blocking key. Higher values generate tighter (more precise) candidate sets but increase index build time. |
| `min_uniqueness_ratio` | `float` | `0.01` | Minimum fraction of unique values a column must have to be considered for blocking. Columns with near-constant values (e.g. a `country` column that is always `"IN"`) are excluded. |
| `max_block_frequency` | `float` | `0.10` | A column is excluded from blocking if its most common single value appears in more than this fraction of rows. Prevents high-frequency values (e.g. `"ACTIVE"`) from generating enormous candidate sets. |
| `max_candidates_per_row` | `int` | `100` | Safety cap on the number of new-file rows evaluated per legacy row during candidate matching. Candidates are ranked by blocking score before truncation. |

### Reporting Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sample_limit` | `int` | `5` | Maximum number of example mismatches stored per column position and per difference signature. |
| `output_directory` | `str` | `"reconciliation_output"` | Directory where all output CSV files are written. Created automatically if it does not exist. |
| `null_token` | `str` | `"<NULL>"` | Internal sentinel value used to represent `None` / `NaN` / empty during comparison. Should not collide with real data values. |

---

## Matching Logic

### Value Representation

The script maintains two separate representations of every value, intentionally kept apart:

| Representation | Function | Used For | Behaviour |
|---|---|---|---|
| **Raw** | `raw_value()` | Final comparison & fingerprinting | No transformation. `"1000"`, `"1000.00"`, and `" 1000"` are all different. |
| **Matching** | `matching_value()` | Blocking & candidate scoring only | `.strip().lower()` applied. `"Ravi "` and `"ravi"` are treated as equal for discovery, but **not** in the final comparison. |

> **Key principle:** A pair of rows can be *discovered* via normalized matching but still report a difference in the final raw comparison. This is intentional — candidate matching widens the search; raw comparison is the ground truth.

---

### Pass 1 — Exact Raw Matching

Every row is hashed (SHA-256) using its raw values joined with the ASCII Unit Separator (`\x1f`). Rows in the new file whose hash matches a legacy row hash are paired immediately. Duplicate rows are handled correctly — each new-file row can only be used once.

### Pass 2 — Candidate (Fuzzy) Matching

Remaining unmatched legacy rows go through blocking:

1. **Blocking column selection** — Columns are scored by uniqueness ratio and maximum single-value frequency. Columns that are too uniform or too dominant are excluded.
2. **Index building** — Lookup indexes are built for all 1-, 2-, and 3-column combinations of the selected blocking columns.
3. **Candidate generation** — For each legacy row, the strongest available blocking combination is used first. If a multi-column block returns candidates, single-column blocks are skipped to avoid an explosion in candidate count.
4. **Scoring** — Each candidate new row is scored by `row_similarity()`: the fraction of columns where the normalized values match.
5. **Selection** — The best-scoring candidate is accepted only if it meets `min_similarity` and beats the second-best by at least `ambiguity_margin`. Otherwise the legacy row is marked `UNRESOLVED` or `AMBIGUOUS`.
6. **Final raw comparison** — The accepted candidate pair is compared using raw values. Any column-level difference is recorded.

### Match Type Labels

| Label | Meaning |
|---|---|
| `EXACT_RAW` | Rows matched via identical SHA-256 fingerprint in Pass 1 |
| `CANDIDATE_RAW_EQUAL` | Matched via candidate scoring, but raw comparison found zero differences |
| `MATCHED` | Matched via candidate scoring with one or more raw differences recorded |
| `UNRESOLVED` | No candidate met `min_similarity` |
| `AMBIGUOUS` | Best candidate did not beat second-best by `ambiguity_margin` |

---

## Output Files

All files are written to `output_directory` (default: `reconciliation_output/`).

---

### `summary.csv`

One-row summary of the entire reconciliation run.

| Column | Type | Description |
|---|---|---|
| `legacy_records` | int | Total rows in the legacy file |
| `new_records` | int | Total rows in the new file |
| `exact_raw_matches` | int | Rows matched by identical raw fingerprint (Pass 1) |
| `candidate_raw_equal` | int | Rows matched via candidates but found identical on raw comparison |
| `mismatched_records` | int | Rows matched via candidates with at least one column difference |
| `unresolved_legacy_records` | int | Legacy rows with no reliable match in the new file |
| `new_only_records` | int | New-file rows that were never matched to any legacy row |
| `legacy_encoding` | str | Encoding used to read the legacy file |
| `new_encoding` | str | Encoding used to read the new file |
| `encoding_mismatch` | bool | `True` when the two files were read with different encodings |

---

### `detailed_mismatches.csv`

One row per matched pair that has at least one column-level difference.

| Column | Type | Description |
|---|---|---|
| `legacy_index` | int | Row index in the legacy DataFrame |
| `new_index` | int | Row index in the new DataFrame |
| `similarity` | float | Normalized matching similarity score (0.0–1.0) used during candidate selection |
| `difference_columns` | str | Pipe-separated list of differing columns, formatted as `position:column_name` |

---

### `column_mismatch_counts.csv`

Aggregated count of how many matched pairs differ at each column position. Sorted descending by mismatch count.

| Column | Type | Description |
|---|---|---|
| `position` | int | 1-based column position |
| `mismatch_count` | int | Number of matched pairs where this column differs |

---

### `difference_signatures.csv`

Groups mismatches by the *combination* of columns that differ. This surfaces systematic patterns (e.g. "1,000 records differ in columns 3 and 7 together").

| Column | Type | Description |
|---|---|---|
| `difference_signature` | str | Pipe-separated list of `position:column_name` tuples identifying the differing columns |
| `record_count` | int | Number of matched pairs sharing this exact signature |

---

### `mismatch_samples.csv`

Up to `sample_limit` example rows per column position where a difference was found. Useful for quick visual inspection of what the differences look like.

| Column | Type | Description |
|---|---|---|
| `position` | int | 1-based column position |
| `legacy_column` | str | Column name in the legacy file |
| `new_column` | str | Column name in the new file (may differ from legacy) |
| `legacy_row` | int | Row index in the legacy DataFrame |
| `new_row` | int | Row index in the new DataFrame |
| `similarity` | float | Candidate similarity score for this pair |
| `legacy_value` | str | Raw value from the legacy file |
| `new_value` | str | Raw value from the new file |

---

### `unresolved_records.csv`

Legacy rows for which no reliable match could be found in the new file.

| Column | Type | Description |
|---|---|---|
| `legacy_index` | int | Row index in the legacy DataFrame |
| `best_similarity` | float | Highest similarity score found among any candidate (even if below threshold) |
| `second_best_similarity` | float | Second-highest similarity score (0.0 if only one candidate) |
| `candidate_count` | int | Number of candidates evaluated for this row |
| `status` | str | Either `UNRESOLVED` (below min_similarity) or `AMBIGUOUS` (margin too small) |

---

### `new_only_records.csv`

New-file rows that were never paired with any legacy row — potential insertions in the new system.

| Column | Type | Description |
|---|---|---|
| `new_index` | int | Row index in the new DataFrame |

---

### `column_name_differences.csv`

Documents column name differences between the two files at the same position. Empty if column names are identical everywhere.

| Column | Type | Description |
|---|---|---|
| `position` | int | 1-based column position |
| `legacy_column` | str | Column name in the legacy file at this position |
| `new_column` | str | Column name in the new file at this position |

---

## Key Design Decisions

- **Column position is authoritative.** Column names may differ between files; comparison always uses positional alignment. A column count mismatch raises a hard error.
- **Raw values are never mutated for comparison.** All normalization (strip, lowercase) is isolated to candidate discovery only. The final mismatch report always reflects the original CSV values.
- **Quoted fields and headers are handled transparently.** The `quotechar` and `doublequote` parameters tell pandas how to strip enclosing quotes during CSV parsing. After loading, values in the DataFrames never contain the surrounding quotes — the raw comparison therefore sees clean content, not `"value"` vs `value`.
- **Cross-encoding comparison is valid.** Both files are decoded to Python Unicode strings independently before any comparison. Encoding only affects disk-to-memory reading; it has no bearing on string equality once loaded.
- **Encoding differences are flagged, not blocked.** The `encoding_mismatch` field in `summary.csv` and the `ENCODING` section of the console report make the discrepancy visible without preventing the run.
- **Duplicate rows are supported.** Each new-file row can only be consumed once, even if its hash appears multiple times.
- **Blocking is automatic.** No business keys need to be configured. The script infers useful blocking columns from the data's statistical properties.
- **Similarity is for ranking only.** The similarity score determines *which* new row is the best match for a legacy row. It does not determine *whether* individual column values differ — that is always a strict raw equality check.
