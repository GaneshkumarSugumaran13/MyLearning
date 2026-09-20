# CSV Reconciliation Script — Reference Guide

## Overview

This script compares two CSV files — one exported from a **legacy system** and one from a **current/new system** — to identify exact matches, value-level mismatches, unmatched records, and records that exist only in the new file. It uses a two-pass matching strategy (exact fingerprint matching, then candidate-based fuzzy matching via blocking) and produces a single consolidated CSV report.

---

## Usage

Only the two file paths are required. Encodings are auto-detected when not supplied.

```bash
# Minimal — encodings auto-detected via chardet
python recon.py legacy.csv current.csv

# Explicit encodings
python recon.py legacy.csv current.csv --legacy-encoding utf-8 --new-encoding utf-16

# With optional tuning flags
python recon.py legacy.csv current.csv \
    --legacy-encoding latin-1 \
    --new-encoding utf-8 \
    --delimiter ";" \
    --output my_results \
    --min-similarity 0.85 \
    --quotechar "'"
```

**All flags:**

| Flag | Default | Description |
|---|---|---|
| `--legacy-encoding ENCODING` | *(auto-detected)* | Encoding of the legacy file, e.g. `utf-8`, `utf-16`, `latin-1`. Auto-detected via chardet when omitted. |
| `--new-encoding ENCODING` | *(auto-detected)* | Encoding of the new file. Auto-detected when omitted. |
| `--delimiter CHAR` | `,` | Field delimiter character |
| `--output DIR` | `reconciliation_output` | Directory for all output files |
| `--min-similarity FLOAT` | `0.80` | Minimum similarity threshold for candidate acceptance |
| `--ambiguity-margin FLOAT` | `0.05` | Minimum gap between best and second-best candidate scores |
| `--max-block-columns INT` | `3` | Max columns combined into a single blocking key |
| `--max-candidates INT` | `100` | Max candidate rows evaluated per legacy row |
| `--sample-limit INT` | `5` | Max samples stored per column in the console report |
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
| `encoding` | `str` | `"utf-8"` | Shared fallback used when neither per-file encoding is set and chardet is inconclusive |
| `legacy_encoding` | `str` | `""` | Resolved encoding for the legacy file. Populated by `load_csv` — either from the CLI flag or chardet auto-detection. |
| `new_encoding` | `str` | `""` | Resolved encoding for the new file. Same resolution logic as `legacy_encoding`. |

**Encoding resolution order** (applied independently for each file):

1. CLI flag supplied (`--legacy-encoding` / `--new-encoding`) → use it directly, skip detection.
2. No flag → chardet reads the first 64 KB of the file as raw bytes and detects the encoding.
3. chardet inconclusive → fall back to `encoding` (default `utf-8`).

The resolved value is written back into `config.legacy_encoding` / `config.new_encoding` immediately after `load_csv` so that `reconcile()`, `detect_eol()`, and the unified report all see the same resolved encoding — never a blank placeholder.

> **Cross-encoding comparison is safe.** `pd.read_csv` decodes each file from bytes to Python Unicode strings before any comparison takes place. Once both DataFrames are in memory, the source encoding is irrelevant — all values are plain `str`. The only risk is passing the *wrong* encoding, which would produce garbled values during decode. The `encoding_mismatch` flag surfaces the discrepancy so reviewers can verify it is intentional.

### Quote Handling Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `quotechar` | `str` | `'"'` | Character used to wrap field values and column headers. Enclosing quotes are stripped on load; inner content is preserved exactly. |
| `doublequote` | `bool` | `True` | When `True`, `""` inside a quoted field is treated as a single literal `"`. When `False`, the quote character has no special meaning inside a field. |

Both parameters apply equally to column headers and data values — no separate handling is needed for quoted headers.

### Candidate Matching Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `min_similarity` | `float` | `0.80` | Minimum fraction of columns that must match (normalised) for a candidate to be considered. Range 0.0–1.0. |
| `ambiguity_margin` | `float` | `0.05` | Best candidate must beat second-best by at least this margin. If the gap is smaller the match is marked `AMBIGUOUS`. |
| `max_block_columns` | `int` | `3` | Maximum number of columns combined into one blocking key. Higher values produce tighter candidate sets at the cost of index build time. |
| `min_uniqueness_ratio` | `float` | `0.01` | Minimum fraction of unique values a column must have to qualify for blocking. Near-constant columns (e.g. `country = "IN"` everywhere) are excluded. |
| `max_block_frequency` | `float` | `0.10` | A column is excluded from blocking if its most common value appears in more than this fraction of rows. |
| `max_candidates_per_row` | `int` | `100` | Safety cap on candidates evaluated per legacy row. Candidates are ranked by blocking score before truncation. |

### Reporting Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sample_limit` | `int` | `5` | Max example mismatches shown per column in the **console** report. Does not affect the unified CSV report, which stores exactly 1 sample per distinct error type. |
| `output_directory` | `str` | `"reconciliation_output"` | Directory for all output files. Created automatically if absent. |
| `null_token` | `str` | `"<NULL>"` | Internal sentinel for `None` / `NaN` / empty. Must not collide with real data values. |

---

## Functions Reference

### Encoding & Loading

| Function | Purpose |
|---|---|
| `detect_encoding(filepath)` | Reads up to 64 KB of raw bytes and calls `chardet.detect()`. Returns `(encoding_name, confidence)`. |
| `resolve_encoding(explicit, filepath, label)` | Single decision point: uses explicit value if supplied, otherwise calls `detect_encoding`, otherwise falls back to `utf-8`. Prints one status line per file. Returns `(encoding, source)` where source is `"explicit"`, `"detected"`, or `"default"`. |
| `load_csv(config)` | Calls `resolve_encoding` for both files, writes resolved encodings back into `config`, then reads both CSVs via `pd.read_csv` with quoting and encoding applied. |

### EOL Detection

| Function | Purpose |
|---|---|
| `detect_eol(filepath, encoding)` | Two-phase scan. **Phase 1** reads raw bytes to count CRLF, pure LF, and pure CR occurrences and determine the file-level EOL style (`CRLF`, `LF`, `CR`, `MIXED`, or `NONE`). **Phase 2** re-reads the file through pandas to find any embedded newlines (`\n` or `\r`) inside individual field values. Returns a dict with `file_eol`, `eol_counts`, and `embedded_newline_rows`. |

### Matching Pipeline

| Function | Purpose |
|---|---|
| `validate_structure(legacy_df, new_df)` | Compares column counts and names position by position. Column count mismatch raises a hard error. Name differences are recorded but do not block the run. |
| `raw_value(value, null_token)` | Returns the original string with no transformation. Used for fingerprinting and final comparison. |
| `matching_value(value, null_token)` | Returns `.strip().lower()`. Used only for candidate discovery — never for final comparison. |
| `build_matching_dataframe(df, null_token)` | Builds a normalised copy of a DataFrame for blocking. The original is never mutated. |
| `row_fingerprint(row, null_token)` | SHA-256 hash of all raw values joined by `\x1f`. Used in Pass 1. |
| `find_exact_raw_matches(legacy_df, new_df, config)` | Pass 1. Matches rows whose fingerprints are identical. Handles duplicates: each new-file row is consumed at most once. |
| `select_blocking_columns(normalized_df, config)` | Scores columns by uniqueness ratio and maximum single-value frequency. Returns columns suitable for blocking, sorted by descending uniqueness. |
| `build_block_indexes(normalized_new_df, blocking_columns)` | Builds lookup dicts for all 1-, 2-, and 3-column combinations of blocking columns. |
| `generate_candidates(legacy_row, ...)` | Looks up candidate new-file rows using the strongest available blocking key. Falls back to weaker keys only if stronger ones yield nothing. |
| `row_similarity(legacy_row, new_row, config)` | Fraction of columns where normalised values match. Used only for ranking candidates, not for determining mismatch. |
| `find_best_candidate(legacy_row, new_df, candidate_indexes, config)` | Scores all candidates, selects the best if it clears `min_similarity` and beats second-best by `ambiguity_margin`. Returns status `MATCHED`, `UNRESOLVED`, or `AMBIGUOUS`. |
| `compare_rows_raw(legacy_row, new_row, ...)` | Final comparison using raw values only. No trimming, casing, numeric, or date conversion. Every representation difference is a mismatch. |
| `difference_signature(differences)` | Produces a tuple of `(position, legacy_col_name, new_col_name)` for each differing column. Used to group rows by mismatch pattern. |
| `reconcile(legacy_df, new_df, config)` | Orchestrates both passes, accumulates all statistics, and returns the full results dict. |

### Reporting & Export

| Function | Purpose |
|---|---|
| `print_report(results, structure_result)` | Prints a console summary: record counts, encoding section, column structure, column mismatch counts, difference signatures. |
| `print_samples(results, limit)` | Prints example mismatch rows to the console, grouped by column position. |
| `export_unified_csv(results, structure_result, config, legacy_df, new_df, legacy_eol, new_eol)` | Writes the single consolidated report file. See the Output section below. |

---

## Matching Logic

### Value Representation

The script maintains two separate representations of every value, kept strictly apart:

| Representation | Function | Used For | Behaviour |
|---|---|---|---|
| **Raw** | `raw_value()` | Fingerprinting and final comparison | No transformation. `"1000"`, `"1000.00"`, and `" 1000"` are all different. |
| **Matching** | `matching_value()` | Blocking and candidate scoring only | `.strip().lower()` applied. `"Ravi "` and `"ravi"` are treated as equal for discovery but not in the final comparison. |

> **Key principle:** A row pair can be *discovered* via normalised matching but still show differences in the final raw comparison. Candidate matching widens the search; raw comparison is always the ground truth.

### Pass 1 — Exact Raw Matching

Every row is hashed (SHA-256) using its raw values joined with the ASCII Unit Separator (`\x1f`). Rows in the new file whose hash matches a legacy row hash are paired immediately. Each new-file row is consumed at most once, so duplicate rows are handled correctly.

### Pass 2 — Candidate (Fuzzy) Matching

Remaining unmatched legacy rows go through blocking:

1. **Column selection** — Columns are scored by uniqueness ratio and dominant-value frequency. Too-uniform or too-dominant columns are excluded.
2. **Index building** — Lookup dicts are built for all 1-, 2-, and 3-column combinations of the selected blocking columns.
3. **Candidate generation** — The strongest blocking combination is tried first. If a multi-column block yields candidates, single-column blocks are skipped.
4. **Scoring** — Each candidate is scored by `row_similarity()`: fraction of columns where normalised values match.
5. **Selection** — The top candidate is accepted only if it meets `min_similarity` and beats second-best by `ambiguity_margin`. Otherwise the row is marked `UNRESOLVED` or `AMBIGUOUS`.
6. **Final raw comparison** — The accepted pair is compared with raw values. Every column-level difference is recorded with the exact legacy and new values.

### Match Type Labels

| Label | Meaning |
|---|---|
| `EXACT_RAW` | Matched via identical SHA-256 fingerprint in Pass 1 |
| `CANDIDATE_RAW_EQUAL` | Matched via candidate scoring; raw comparison found zero differences |
| `MATCHED` | Matched via candidate scoring with one or more raw differences |
| `UNRESOLVED` | No candidate reached `min_similarity` |
| `AMBIGUOUS` | Best candidate did not beat second-best by `ambiguity_margin` |

### Error Type Keying

Each difference is keyed on `(column_position, legacy_value, new_value)`. This means:

- `amount: "1500.00" vs "1500.0"` and `amount: "" vs "0.00"` are **two separate error types** even though both are in the `amount` column.
- Exactly **one sample row pair** is stored per error type, scanning the entire file before selecting it.
- The unified CSV report shows a separate block for each error type, so the same column name can appear multiple times — once per distinct mismatch pattern.

---

## Output

All files are written to `output_directory` (default: `reconciliation_output/`).

### `reconciliation_report.csv` — Unified Single Report

The primary output. One file with five clearly labelled sections separated by `### SECTION NAME ###` header rows. Open in Excel or any spreadsheet tool; the section headers make it easy to navigate.

#### Section 1 — ENCODING

One-line summary followed by a two-column table.

| Column | Description |
|---|---|
| `field` | Metadata label (`legacy_file`, `legacy_encoding`, `new_file`, `new_encoding`) |
| `value` | The corresponding value |

The `>>` summary line reads either:
- `ENCODING MISMATCH: legacy=X vs new=Y. Comparison is valid — both files decoded to Unicode before comparison — but verify this is intentional.`
- `Encodings match — no encoding difference detected.`

#### Section 2 — SCHEMA MISMATCHES (COLUMN NAME DIFFERENCES)

The `>>` summary line lists all differing column names in a single sentence. If differences exist, a table follows:

| Column | Description |
|---|---|
| `position` | 1-based column position |
| `legacy_column_name` | Column name in the legacy file at this position |
| `new_column_name` | Column name in the new file at this position |

#### Section 3 — COUNT MISMATCH

The `>>` summary line states whether counts match and by how much if not. Followed by a metrics table:

| Row label | Description |
|---|---|
| `total_records` | Total rows loaded from each file |
| `exact_matches` | Rows matched by identical fingerprint |
| `mismatched_records` | Matched pairs with at least one column difference (legacy side) |
| `unresolved_legacy` | Legacy rows with no reliable match |
| `new_only_records` | New-file rows never matched to any legacy row |

#### Section 4 — NEWLINE / EOL DIFFERENCES

Two sub-sections.

**File-level EOL table:**

| Column | Description |
|---|---|
| `source` | `legacy` or `new` |
| `file_eol` | Dominant line-ending style: `CRLF`, `LF`, `CR`, `MIXED`, or `NONE` |
| `CRLF_count` | Number of `\r\n` sequences in the file |
| `LF_count` | Number of `\n` not preceded by `\r` |
| `CR_count` | Number of `\r` not followed by `\n` |

**Embedded newline table** (when any exist):

| Column | Description |
|---|---|
| `data_row` | 1-based row number in the file (including header) |
| `column` | Column name where the embedded newline was found |
| `raw_value` | `repr()` of the field value, showing the `\n` or `\r` visibly |

#### Section 5 — MISMATCHED DATA

One block per distinct `(column, error type)` combination. Blocks for the same column are grouped consecutively. Within each column, blocks are sorted by `(legacy_value, new_value)`.

Before the first block for each column, a `>>` summary line states:

```
Column 'amount' (position 3): 2 distinct error type(s) across 2 mismatched record(s).
```

Each block contains:

| Row | Content |
|---|---|
| Column name | The column name (or `legacy_name / new_name` if names differ) used as a block header |
| Error type line | `error_type: legacy='X' vs new='Y' \| occurrences=N` |
| Header row | All column names from the legacy file |
| `[LEGACY]` row | Complete raw values of the legacy row for this sample |
| `[NEW]` row | Complete raw values of the new row for this sample |

The `[LEGACY]` / `[NEW]` prefix is prepended as an extra first column so the source is unambiguous when opening in a spreadsheet.

---

## Key Design Decisions

- **Column position is authoritative.** Column names may differ; comparison always uses positional alignment. A column count mismatch raises a hard error before any matching begins.
- **Raw values are never mutated for comparison.** All normalisation (strip, lowercase) is isolated to candidate discovery. The final report always shows original CSV values.
- **Quoted fields and headers are handled transparently.** After `pd.read_csv` strips enclosing quotes, values in the DataFrames are clean strings. The raw comparison therefore sees `value` not `"value"`.
- **Encoding is auto-detected when not supplied.** chardet reads the first 64 KB as raw bytes. UTF-16 files are identified instantly from the BOM. The resolved encoding is written back into `config` so every subsequent step (EOL scan, summary, report) uses the same value.
- **Cross-encoding comparison is valid.** Encoding only affects disk-to-memory decoding. Once loaded, both DataFrames hold plain Python `str` regardless of source encoding.
- **Encoding differences are flagged, not blocked.** The run always completes; the mismatch is surfaced in the report for human review.
- **One sample per error type, across the entire file.** The script scans all rows before selecting the sample for each `(column, legacy_value, new_value)` key. This means the sample is always a real representative, not just the first row encountered.
- **Same column, different patterns → separate blocks.** `amount: "1500.00" vs "1500.0"` and `amount: "" vs "0.00"` produce two distinct blocks under the `amount` heading, making it easy to see that there are two separate data quality issues rather than one.
- **Duplicate rows are supported.** Each new-file row is consumed at most once across both passes.
- **Blocking is fully automatic.** No business keys need to be configured. The script infers useful blocking columns from uniqueness and frequency statistics.
- **Similarity is for ranking only.** The similarity score determines which new row is the best match candidate. Whether individual column values differ is always decided by strict raw string equality.
