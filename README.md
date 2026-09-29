# CSV Reconciliation Tool (`recon.py`)

Compare a **legacy** CSV export against a **new** CSV export and get a single, sectioned CSV report describing every kind of difference: encoding, schema, row counts, unmatched rows, malformed lines, line endings, cell-level mismatches (classified by type), and numeric rollup totals.

The tool is designed for data-migration and system-cutover validation, where the question is *"is the new file exactly what the old file was, and if not, what precisely changed?"*

---

## Table of Contents

1. [Key Concepts](#1-key-concepts)
2. [Requirements & Installation](#2-requirements--installation)
3. [Quick Start](#3-quick-start)
4. [Command-Line Reference](#4-command-line-reference)
5. [Usage Scenarios & Script Calls](#5-usage-scenarios--script-calls)
6. [How Matching Works](#6-how-matching-works)
7. [Difference Classification](#7-difference-classification)
8. [Cases Handled (with Examples)](#8-cases-handled-with-examples)
9. [Output: Console](#9-output-console)
10. [Output: The Unified CSV Report](#10-output-the-unified-csv-report)
11. [Numeric Rollup Validation](#11-numeric-rollup-validation)
12. [Using It as a Python Library](#12-using-it-as-a-python-library)
13. [Configuration Reference](#13-configuration-reference)
14. [Limitations & Known Issues](#14-limitations--known-issues)
15. [Troubleshooting / FAQ](#15-troubleshooting--faq)

---

## 1. Key Concepts

| Concept | Meaning |
|---|---|
| **Raw comparison** | The final verdict on a cell is always made on the **exact text** in the file. No trimming, lowercasing, number parsing, or date parsing. `1000` ≠ `1000.00`, `Ravi ` ≠ `ravi`. |
| **Normalized matching** | Used **only to find which legacy row pairs with which new row** (blocking + similarity). It never decides whether values are equal. |
| **Column position is authoritative** | Columns are compared by position, not by name. Name differences are reported but don't block comparison. |
| **Row order doesn't matter** | Rows are paired by content, not by line number. Duplicate rows are handled with correct multiplicity. |
| **Every difference is a mismatch** | Classification (e.g. `TRAILING_ZEROS_ONLY`) explains *why* values differ but never downgrades a mismatch to a match. |
| **Everything is read as text** | `dtype=str` and `keep_default_na=False`, so leading zeros, trailing zeros and empty strings survive loading. |

---

## 2. Requirements & Installation

- **Python** 3.8+ (3.9+ recommended)
- **pandas** ≥ 1.4 (required for the callable `on_bad_lines` with the Python engine)
- **chardet**

```bash
pip install "pandas>=1.4" chardet
```

Save the script as `recon.py` (the name used throughout this document).

> ⚠️ **Working-directory restriction.** Every input file **and** the output directory must resolve to a location **inside the current working directory** (symlinks are resolved). Otherwise the script raises `ValueError: Path outside working directory`. Run the script from a folder that contains (or is a parent of) your files.

---

## 3. Quick Start

```bash
cd /path/to/folder/containing/both/files
python recon.py legacy.csv new.csv --min-similarity 0.75
```

What happens:

1. Encodings, delimiters and malformed lines are detected/handled.
2. Rows are paired (exact match first, then fuzzy candidate matching).
3. Paired rows are compared cell by cell on raw values.
4. A report is written to `reconciliation_output/<common_name>_recon_report.csv`.

> See [Known Issues](#14-limitations--known-issues): the CLI's built-in default for `--min-similarity` is `-0.75`, which effectively disables the similarity threshold. Pass `--min-similarity 0.75` (or `0.80`) explicitly.

---

## 4. Command-Line Reference

```text
python recon.py LEGACY_FILE NEW_FILE [options]
```

### Positional arguments

| Argument | Description |
|---|---|
| `legacy_file` | Path to the legacy (source-of-truth) CSV. |
| `new_file` | Path to the new/current CSV. |

### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--legacy-encoding ENC` | str | *auto-detect* | Encoding of the legacy file (e.g. `utf-8`, `utf-16`, `latin-1`, `cp1252`). |
| `--new-encoding ENC` | str | *auto-detect* | Encoding of the new file. |
| `--delimiter CHAR` | str | *auto-detect* | Field delimiter applied to **both** files. Auto-detection chooses among `, \| ; <tab>` per file. |
| `--output DIR` | str | `reconciliation_output` | Output directory (must be inside the working directory; created if missing). |
| `--min-similarity F` | float | `-0.75` (CLI) / `0.75` (config) | Minimum normalized row similarity (0–1) for a candidate pair to be accepted. **Pass this explicitly.** |
| `--ambiguity-margin F` | float | `0.05` | If the best and second-best candidates are closer than this margin (on both normalized and raw similarity), the row is `AMBIGUOUS` and left unpaired. |
| `--max-block-columns N` | int | `3` | Maximum number of columns combined into one blocking key. |
| `--max-candidates N` | int | `100` | Maximum candidate rows evaluated per legacy row. |
| `--sample-limit N` | int | `5` | Accepted for compatibility; currently **not used** (one sample row pair is stored per error type). |
| `--quotechar CHAR` | str | `"` | Character wrapping quoted fields. |
| `--no-doublequote` | flag | off | Disable `""` → `"` handling inside quoted fields. |
| `--max-grouping-cardinality N` | int | `20` | Max distinct values a text column may have to be used as a grouping column in the rollup. |
| `--precision N` | int | `2` | Decimal places used to detect `PRECISION_MISMATCH`. |

Show help:

```bash
python recon.py --help
```

---

## 5. Usage Scenarios & Script Calls

### 5.1 Basic comparison (everything auto-detected)

```bash
python recon.py legacy.csv new.csv --min-similarity 0.75
```

Encoding (chardet/BOM) and delimiter (`csv.Sniffer`) are detected per file.

### 5.2 Explicit encodings (one file UTF-8, the other UTF-16)

```bash
python recon.py legacy.csv new.csv \
    --legacy-encoding utf-8 \
    --new-encoding utf-16 \
    --min-similarity 0.75
```

Only one side explicit? The other is still auto-detected:

```bash
python recon.py legacy.csv new.csv --new-encoding latin-1 --min-similarity 0.75
```

### 5.3 Files with a UTF-8 BOM or UTF-16 BOM

No flags needed — a BOM is recognized immediately (`utf-8-sig` / `utf-16`):

```bash
python recon.py legacy_bom.csv new.csv --min-similarity 0.75
```

### 5.4 Pipe-delimited files

```bash
python recon.py legacy.txt new.txt --delimiter "|" --min-similarity 0.75
```

### 5.5 Semicolon-delimited files (common in European exports)

```bash
python recon.py legacy.csv new.csv --delimiter ";" --min-similarity 0.75
```

### 5.6 Tab-delimited files

```bash
# bash / zsh
python recon.py legacy.tsv new.tsv --delimiter $'\t' --min-similarity 0.75

# or rely on auto-detection
python recon.py legacy.tsv new.tsv --min-similarity 0.75
```

> `--delimiter` applies to **both** files. If the two files use different delimiters, omit the flag and let auto-detection choose per file.

### 5.7 Files with different delimiters

```bash
# legacy is comma-separated, new is pipe-separated
python recon.py legacy.csv new_pipe.csv --min-similarity 0.75
```

The console prints `delimiter: legacy=',', new='|' (auto-detected)`.

### 5.8 Single-quote-wrapped fields

```bash
python recon.py legacy.csv new.csv --quotechar "'" --min-similarity 0.75
```

### 5.9 Disable doubled-quote escaping

```bash
python recon.py legacy.csv new.csv --no-doublequote --min-similarity 0.75
```

### 5.10 Custom output directory

```bash
python recon.py legacy.csv new.csv --output results/2026-09-29 --min-similarity 0.75
```

### 5.11 Stricter matching (fewer, safer pairings)

```bash
python recon.py legacy.csv new.csv \
    --min-similarity 0.90 \
    --ambiguity-margin 0.10 \
    --max-block-columns 3 \
    --max-candidates 50
```

Higher threshold/margin → more rows reported as `UNRESOLVED`/`AMBIGUOUS` and fewer risky pairings.

### 5.12 Looser matching (heavily changed rows)

```bash
python recon.py legacy.csv new.csv \
    --min-similarity 0.60 \
    --ambiguity-margin 0.02 \
    --max-candidates 200
```

Lower threshold → more rows are paired and shown as mismatches instead of unresolved. Review results carefully; low thresholds can pair unrelated rows.

### 5.13 Higher precision checking

```bash
python recon.py legacy.csv new.csv --precision 4 --min-similarity 0.75
```

`10.12349` vs `10.12341` becomes `PRECISION_MISMATCH` (agree on first 4 decimals) instead of `EXACT_VALUES`.

### 5.14 Control rollup grouping columns

```bash
# allow text columns with up to 50 distinct values as grouping columns
python recon.py legacy.csv new.csv --max-grouping-cardinality 50 --min-similarity 0.75

# fewer, coarser groupings
python recon.py legacy.csv new.csv --max-grouping-cardinality 8 --min-similarity 0.75
```

### 5.15 Files in subfolders

```bash
python recon.py data/legacy/p_accounts.csv data/new/a_accounts.csv --min-similarity 0.75
```

Both paths must be inside the current directory. To compare files elsewhere, copy them under the working directory or `cd` to a common parent.

### 5.16 Everything together

```bash
python recon.py extracts/p_customers.csv extracts/a_customers.csv \
    --legacy-encoding cp1252 \
    --new-encoding utf-8 \
    --delimiter ";" \
    --quotechar '"' \
    --min-similarity 0.80 \
    --ambiguity-margin 0.05 \
    --max-block-columns 3 \
    --max-candidates 100 \
    --precision 2 \
    --max-grouping-cardinality 20 \
    --output out/customers
```

---

## 6. How Matching Works

### Pass 1 — Exact raw matching

Each row is hashed (SHA-256 over the raw values joined with `\x1f`). Legacy rows whose hash exists among unused new rows are paired as `EXACT_RAW`. Duplicates are consumed one-for-one, so 3 identical legacy rows need 3 identical new rows.

### Pass 2 — Candidate matching for the leftovers

For each remaining legacy row:

1. **Normalize** cells for matching only: `strip()` → lowercase → remove commas → drop trailing zeros in decimals (`1,000.50` ≈ `1000.5`).
2. **Blocking-column selection** (on the new file, checked against legacy). A column qualifies when:
   - uniqueness ratio ≥ `0.01`,
   - its most frequent value is ≤ 10% of rows (**relaxed for files ≤ 50 rows**),
   - ≥ 50% of its values also appear in the other file (cross-file overlap).
   If nothing qualifies, the best-overlapping columns are used as a fallback so blocking is never empty.
3. **Candidate generation**: indexes are built for single columns and combinations (up to `--max-block-columns`). The largest combination is tried first; if it yields candidates the search stops there. If blocking yields nothing and ≤ `--max-candidates` new rows remain, all remaining rows are evaluated.
4. **Scoring**: fraction of columns equal after normalization (raw similarity is the tie-breaker).
5. **Decision** (in order):
   - best < `--min-similarity` → **UNRESOLVED**
   - best and second-best both within `--ambiguity-margin` (normalized *and* raw) → **AMBIGUOUS**
   - an **ID-like column** (see below) differs after normalization → **UNRESOLVED**
   - otherwise → **MATCHED**

### ID-column guard

Columns whose names match the pattern `(^|_)(id|key|code|num|no|number|ref|uuid|guid|seq)($|_)` (case-insensitive) must agree (normalized) for a pair to be accepted. This prevents pairing *"customer 1001"* with *"customer 1002"* just because everything else looks alike.

> Pattern matches `customer_id`, `ID`, `Account_No`, `ref_code`. It does **not** match names with spaces or camel-case such as `Customer ID` or `CustomerID`.

### Final comparison

Matched pairs are compared cell by cell on raw text; every difference is recorded, classified, and counted.

---

## 7. Difference Classification

Each mismatched cell is labelled with one class (whitespace is separated out first):

| Class | Meaning | Legacy | New |
|---|---|---|---|
| `WHITESPACE_ONLY` | Differ only by leading/trailing whitespace | `Ravi` | `Ravi ` |
| `WHITESPACE_AND_<class>` | Whitespace **plus** another difference | `Ravi ` | `ravi` → `WHITESPACE_AND_EXACT_VALUES` |
| `THOUSANDS_SEPARATOR` | Same digits; only comma presence/placement differs | `1,000.50` | `1000.50` |
| `STARTING_ZERO` | Leading zero before the decimal point differs | `0.003` | `.003` |
| `TRAILING_ZEROS_ONLY` | Same fraction after removing trailing zeros (both sides have a decimal point) | `1000.5` | `1000.50` |
| `PRECISION_MISMATCH` | Agree on first *N* decimals (**truncated**, not rounded), but at least one side has non-zero digits beyond *N* | `10.239` | `10.231` (N=2) |
| `EXACT_VALUES` | Any other difference | `Alice` | `Alicia`, or `100.50` vs `100.75` |

Important behaviors:

- `.` is the only decimal point. `,` is never treated as a decimal separator.
- `1000` vs `1000.00` is **`EXACT_VALUES`** (the integer side has no fractional part, so the trailing-zero rule does not apply).
- Case differences (`Ravi` vs `ravi`) are `EXACT_VALUES`.
- Empty strings are preserved as `""` (not converted to null), so `""` vs `NULL` is a mismatch.

The report stores **one sample row pair per (column, class)**, plus the total occurrence count.

---

## 8. Cases Handled (with Examples)

Examples below use small files. `legacy.csv` is the first table, `new.csv` the second.

### 8.1 Identical files (row order shuffled)

```csv
id,name,amount
1,Ravi,100.50
2,Meena,200.00
```
```csv
id,name,amount
2,Meena,200.00
1,Ravi,100.50
```

**Result:** 2 `EXACT_RAW` matches, no mismatches. Row order is irrelevant.

### 8.2 Duplicate rows

Legacy has the row `5,Sam,10.00` three times; new has it twice.

**Result:** 2 exact matches; the third legacy copy is `UNRESOLVED` (no counterpart). Row-count difference is reported in **COUNT MISMATCH**.

### 8.3 Cell-level mismatches

```csv
id,name,amount
1,Ravi,1000.50
2,Meena,0.003
3,Kiran,10.239
```
```csv
id,name,amount
1,Ravi ,1,000.50
2,Meena,.003
3,Kiran,10.231
```
*(quote the `"1,000.50"` value in a real comma-delimited file.)*

**Result:** rows are paired by fuzzy match; report shows:

| Column | Class | Legacy → New |
|---|---|---|
| `name` | `WHITESPACE_ONLY` | `Ravi` → `Ravi ` |
| `amount` | `THOUSANDS_SEPARATOR` | `1000.50` → `1,000.50` |
| `amount` | `STARTING_ZERO` | `0.003` → `.003` |
| `amount` | `PRECISION_MISMATCH` | `10.239` → `10.231` |

### 8.4 Rows only in legacy (missing after migration)

**Result:** listed under **UNRESOLVED RECORDS → LEGACY**, with status and best similarity.

### 8.5 Rows only in new (extra after migration)

**Result:** listed under **UNRESOLVED RECORDS → NEW (unpaired)**.

### 8.6 Ambiguous pairing

Two new rows are nearly identical (e.g. same everything except a comment field) and a legacy row scores equally well against both.

**Result:** status `AMBIGUOUS`; no pairing is forced. Reduce ambiguity by tightening `--min-similarity`, lowering `--ambiguity-margin`, or reviewing the rows manually.

### 8.7 Same row but different ID

Legacy `id=1001, name=Ravi` vs new `id=1002, name=Ravi` (all else equal).

**Result:** ID-guard rejects the pair → `UNRESOLVED` for both sides (only if the column name matches the ID pattern, e.g. `id`, `cust_id`).

### 8.8 Column-name differences (same count)

```csv
cust_id,full_name,amt        <- legacy header
customer_id,name,amount      <- new header
```

**Result:** **SCHEMA MISMATCHES** section lists positions 1–3 with both names. Comparison proceeds by position.

### 8.9 Different column counts

Legacy has 8 columns, new has 9.

**Result:** console warning `column count differs ... Comparing first 8 columns only.` The extra trailing column(s) are excluded from comparison and noted in the schema section.

### 8.10 Different encodings

Legacy `latin-1`, new `utf-8`.

**Result:** **ENCODING** section shows both, flags **ENCODING MISMATCH**. Comparison remains valid because both files are decoded to Unicode first; text that decodes differently (e.g. `é` mis-decoded as `Ã©`) will show up as `EXACT_VALUES` mismatches.

**Automatic fallback:** when no encoding is passed, the detected encoding is verified against the whole file. If it fails to decode (for example a file detected as UTF-8 that contains a `£` byte), the tool falls back to `cp1252`, then `latin-1`, and prints the fallback in the "Resolving file encodings" output. The ENCODING section of the report shows the encoding actually used. Passing `--legacy-encoding` / `--new-encoding` explicitly always skips the check and uses the value you gave.

### 8.11 Different line endings

Legacy uses CRLF, new uses LF (or mixed).

**Result:** **NEWLINE / EOL DIFFERENCES** shows `file_eol` and CRLF/LF/CR counts per file, with a `FILE-LEVEL EOL MISMATCH` note.

### 8.12 Embedded newlines inside quoted fields

```csv
id,notes
1,"line one
line two"
```

**Result:** listed in the EOL section with data row, column, and `repr()` of the value.

### 8.13 Malformed rows (too few or too many delimiters)

```csv
id,name,amount
1,Ravi,100
2,Meena            <- too few fields
3,Kiran,50,extra   <- too many fields
```

**Result:** both rows are dropped from that file, listed under **SKIPPED LINES**. If an unresolved legacy row's ID appears in the skipped text of the new file, its `likely_cause` is set to *"possible counterpart skipped due to delimiter issue"*. Skipped rows are also excluded from rollups (a note is added).

### 8.14 Leading-zero identifiers

`001000` vs `1000` — kept as text, so this is a mismatch (`EXACT_VALUES`), never silently equal.

### 8.15 Small files (≤ 50 rows)

The blocking frequency cap is relaxed and a fallback guarantees blocking columns exist, so tiny files still pair correctly.

---

## 9. Output: Console

Typical run:

```text
Loading CSV files...

Resolving file encodings:
  legacy: utf-8 (auto-detected, chardet saw ascii-only content, confidence=100%)
  new   : utf-16 (auto-detected, confidence=100%)
  delimiter: legacy=',', new=',' (auto-detected)
  legacy: 2 row(s) skipped (delimiter count mismatch)
Legacy DF shape : (1200, 9)
New DF shape    : (1198, 9)

Starting reconciliation...

================================================================================
CSV RECONCILIATION REPORT
================================================================================
Legacy records              : 1,200
New records                 : 1,198
Exact raw matches           : 1,150
...
Unified report written to: reconciliation_output/customers_recon_report.csv
Reconciliation completed.
```

Console sections: record summary → ENCODING → COLUMN STRUCTURE → COLUMN MISMATCH COUNTS → DIFFERENCE SIGNATURES.

The process exits normally (code 0) even when mismatches are found; inspect the report to decide pass/fail.

---

## 10. Output: The Unified CSV Report

**File name:** derived from the longest common substring of the two file names (stripped of `_ - .`), plus `_recon_report.csv`.

| Legacy file | New file | Report |
|---|---|---|
| `p_accounts.csv` | `a_accounts.csv` | `accounts_recon_report.csv` |
| `legacy_orders_2026.csv` | `new_orders_2026.csv` | `orders_2026_recon_report.csv` |
| `foo.csv` | `bar.csv` | `reconciliation_recon_report.csv` |

**Location:** `--output` directory (default `reconciliation_output/`).
**Format:** UTF-8 CSV, every field quoted (`QUOTE_ALL`). Sections are separated by two blank records and headed `### TITLE ###`. Lines starting with `>>` are human-readable summaries.

### Sections (in order)

| # | Section | Contents |
|---|---|---|
| 1 | **ENCODING** | File names, detected/explicit encodings, mismatch note. |
| 2 | **SCHEMA MISMATCHES** | Column-name differences by position; extra trailing columns. |
| 3 | **COUNT MISMATCH** | Record counts, difference, exact matches, mismatched, unresolved, new-only. |
| 4 | **UNRESOLVED RECORDS** | Legacy rows not paired (with status, best similarity) and new rows left over, with original file row numbers. |
| 5 | **SKIPPED LINES** | Rows dropped for delimiter-count problems. |
| 6 | **NEWLINE / EOL DIFFERENCES** | File EOL style + counts; embedded newline occurrences. |
| 7 | **MISMATCHED DATA** | One block per (column, error class): header row, one `[LEGACY]` row, one `[NEW]` row, plus occurrence count. |
| 8 | **NUMERIC ROLLUP VALIDATION** | File-level and grouped SUM/COUNT comparison. |

### Example: mismatched-data block

```text
"amount"
"error_type: THOUSANDS_SEPARATOR | first seen: legacy='1000.50' vs new='1,000.50' | occurrences=42"
"cust_id","name","amount","status"
"[LEGACY]","1","Ravi","1000.50","ACTIVE"
"[NEW]","1","Ravi","1,000.50","ACTIVE"
```

`file_row` values (in unresolved sections) are 1-based file lines including the header (data row 1 = line 2).

---

## 11. Numeric Rollup Validation

Independent of row pairing, totals for **entire files** are compared, which catches lost or altered rows even when pairing is imperfect.

### Numeric column detection (based on the legacy file)

A column is numeric when:

- ≥ 80% of its non-empty values parse as finite decimals (`1000`, `1000.00`, `.04`, `-0.04`, `+1.5`, `1e3`), **and**
- it does not look like an identifier: name matches the ID pattern, **or** >80% of the first 50 values are plain integers.

> ⚠️ Because of the integer heuristic, a column containing only whole numbers (e.g. quantity `1, 2, 5`) is treated as ID-like and **excluded** from rollups. Columns with decimals (e.g. `100.50`) are included.

Non-parsable values (`N/A`, `TBD`) are skipped and counted in a `COUNT (skipped — non-numeric values)` row.

### Grouping column detection

A non-numeric column qualifies when it has ≤ `--max-grouping-cardinality` distinct values (case/space-insensitive), and neither its name nor values look like an ID or date. Note the date-name check is a substring match (`date|time|ts|timestamp|dt|year|month|day`), so names containing `ts` (e.g. `accounts`) can be excluded.

### What is computed

- `SUM` (Python `Decimal`, no float drift) and `COUNT (parsed)` for each numeric column, per file.
- Same, grouped by each grouping column (group keys are case/space-insensitive; blanks and null tokens are ignored).
- Mismatched sums are flagged with ` !`, and grouped mismatches include the delta (`new − legacy`).

### Example

```text
"--- FILE-LEVEL ROLLUP ---"
"metric","amount [LEGACY]","amount [NEW]"
"SUM","250400.75 !","250390.75 !"
"COUNT (parsed)","1200","1198"
">> ROLLUP MISMATCH on 1 column(s): amount. Cells marked with ' !' indicate a difference."

"Grouped by: region"
"region","amount SUM [LEGACY]","amount SUM [NEW]"
"East","100000.00","100000.00"
"West","150400.75 !","150390.75 !"
">> amount: region=West sum diff -10.00"
```

Rollups use the whole loaded frames (rows dropped at load time are excluded), and new-file columns are aligned to legacy by position.

---

## 12. Using It as a Python Library

The main block is only a thin driver; the functions can be reused. Run from a directory that contains the files.

```python
from recon import (
    ReconciliationConfig, load_csv, validate_structure, reconcile,
    detect_eol, identify_numeric_columns, identify_grouping_columns,
    compute_rollups, export_unified_csv, print_report,
)
import recon

config = ReconciliationConfig(
    legacy_file="legacy.csv",
    new_file="new.csv",
    min_similarity=0.80,
    ambiguity_margin=0.05,
    output_directory="out",
)
recon._PRECISION_DIGITS = config.precision_digits   # used by the classifier

legacy_df, new_df = load_csv(config)                # also resolves encodings/delimiters
structure = validate_structure(legacy_df, new_df)

# Align to common columns if counts differ (the CLI does this for you)
common = min(structure["legacy_column_count"], structure["new_column_count"])
legacy_df, new_df = legacy_df.iloc[:, :common], new_df.iloc[:, :common]

results = reconcile(legacy_df, new_df, config)
print(results["summary"])

legacy_eol = detect_eol(config.legacy_file, config.legacy_encoding, config.legacy_delimiter)
new_eol    = detect_eol(config.new_file,    config.new_encoding,    config.new_delimiter)

num_cols = identify_numeric_columns(legacy_df, config.null_token)
grp_cols = identify_grouping_columns(legacy_df, num_cols, config)
new_aligned = new_df.copy(); new_aligned.columns = legacy_df.columns
rollup = compute_rollups(legacy_df, new_aligned, num_cols, grp_cols, config.null_token)

path = export_unified_csv(results, structure, config, legacy_df, new_df,
                          legacy_eol, new_eol, rollup=rollup)
```

Key `results` fields: `summary`, `matched_pairs`, `detailed_results`, `unresolved_records`, `new_only_records`, `column_mismatch_counts`, `difference_signature_counts`, `column_error_type_counts`, `samples_by_error_type`, `blocking_columns`.

Summary keys: `legacy_records`, `new_records`, `exact_raw_matches`, `candidate_raw_equal`, `mismatched_records`, `unresolved_legacy_records`, `new_only_records`, `legacy_encoding`, `new_encoding`, `encoding_mismatch`.

---

## 13. Configuration Reference

`ReconciliationConfig` fields (CLI flags map to these):

| Field | Default | Purpose |
|---|---|---|
| `legacy_file`, `new_file` | required | Input paths. |
| `delimiter` | `""` | Shared delimiter; blank = auto-detect per file. |
| `legacy_encoding`, `new_encoding` | `""` | Blank = auto-detect. Resolved values are written back into the config by `load_csv`. |
| `encoding` | `"utf-8"` | Legacy shared fallback field (not used by the loader). |
| `quotechar` / `doublequote` | `"` / `True` | Quote handling. |
| `precision_digits` | `2` | Digits for `PRECISION_MISMATCH`. |
| `min_similarity` | `0.75` | Candidate acceptance threshold. |
| `ambiguity_margin` | `0.05` | Required lead of best over second-best. |
| `max_block_columns` | `3` | Max columns per blocking key. |
| `min_uniqueness_ratio` | `0.01` | Blocking column uniqueness floor. |
| `max_block_frequency` | `0.10` | Max share for a single blocking value (large files). |
| `max_candidates_per_row` | `100` | Candidate cap per legacy row. |
| `small_file_row_threshold` | `50` | ≤ this many rows → relaxed blocking. |
| `min_cross_file_overlap` | `0.5` | Required value overlap between files for blocking columns. |
| `sample_limit` | `5` | Currently unused. |
| `output_directory` | `reconciliation_output` | Report location. |
| `null_token` | `<NULL>` | Internal NULL placeholder. |
| `max_grouping_cardinality` | `20` | Rollup grouping limit. |

Internal constants: `_DETECT_SAMPLE_BYTES = 10_000_000` (bytes fed to chardet), `_NUMERIC_THRESHOLD = 0.80`.

---

## 14. Limitations & Known Issues

- **CLI `--min-similarity` default is `-0.75`** (the source has `default=-0.75`, apparently a typo for `0.75`/`0.80`; the help text says 0.80). With a negative threshold, the similarity check never rejects a candidate, so weak pairs may be matched. **Always pass `--min-similarity` explicitly**, or fix the default in `parse_args()`.
- `--sample-limit` is parsed but not used.
- **Memory & speed:** both files are fully loaded; matching uses `iterrows`, and EOL detection loops over raw bytes in Python. Expect slow runs on very large files (hundreds of MB / millions of rows).
- Fuzzy pass is O(unmatched legacy rows × candidates); huge numbers of non-identical rows are the slowest case.
- The files must be under the current working directory (see [Requirements](#2-requirements--installation)).
- `1000` vs `1000.00` classifies as `EXACT_VALUES`, not `TRAILING_ZEROS_ONLY`.
- Integer-only numeric columns are excluded from rollups by the ID heuristic; ID detection depends on column-name patterns (`Customer ID` with a space is not matched, `customer_id` is).
- Short-line dropping maps file record numbers to DataFrame indices; if pandas also skips over-long lines earlier in the file, the mapping can shift. Review the SKIPPED LINES section on files with many malformed rows.
- Columns are compared positionally; if the same columns are simply reordered, they'll appear as mismatches.
- Console output for the rollup is not printed; rollups appear only in the CSV report.
- The docstring for `export_unified_csv` lists five sections; the report actually contains the eight sections described above.
- Fallback decoding (`cp1252` / `latin-1`) reads any byte sequence without error, so a UTF-8 file with a few stray Latin-1 bytes will load, but the affected characters may look wrong in the report. If accented or currency characters look garbled, pass the encoding explicitly.

---

## 15. Troubleshooting / FAQ

**`ValueError: Path outside working directory`**
Run from a folder containing both files and the output directory, or use relative paths inside it.

**`FileNotFoundError: Not a file: ...`**
Wrong path, or the path is a directory.

**Garbled characters (`Ã©`, `�`) in mismatches**
Wrong encoding chosen. Pass `--legacy-encoding` / `--new-encoding` explicitly (`cp1252`, `latin-1`, `utf-8`, `utf-16`).

**`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa3 in position N: invalid start byte`**
One of the files is not UTF-8. Byte `0xa3` is the `£` sign in Windows-1252 / Latin-1. This happens when `--legacy-encoding utf-8` / `--new-encoding utf-8` was passed for a file that isn't UTF-8, or when auto-detection was inconclusive and defaulted to UTF-8. The "Resolving file encodings" lines printed at the start of the run show which file and which encoding were used. Pass the correct encoding for the failing file:

```bash
python recon.py legacy.csv new.csv --legacy-encoding cp1252 --min-similarity 0.75
python recon.py legacy.csv new.csv --new-encoding cp1252 --min-similarity 0.75
```

Use `cp1252` first for files produced on Windows. If it also fails, use `latin-1`, which maps every byte and never fails to decode. Other common bytes: `0xe9` (`é`), `0x80` (`€` in cp1252), `0x92` (curly apostrophe in cp1252).

**Everything shows as one column / wrong column count**
Delimiter mis-detected. Pass `--delimiter` explicitly (`,`, `";"`, `"|"`, `$'\t'`).

**Lots of `UNRESOLVED` rows that clearly correspond**
Lower `--min-similarity` (e.g. `0.6`), reduce `--ambiguity-margin`, or check whether an ID-like column (`*_id`, `*_no`, `*_code`) legitimately changed between systems (the ID guard rejects such pairs).

**Rows paired that shouldn't be**
Raise `--min-similarity` (e.g. `0.9`) and `--ambiguity-margin`.

**Many `AMBIGUOUS` rows**
The data has near-duplicate rows. Add distinguishing data or accept manual review of those rows.

**Rollup section says "No numeric columns identified"**
No column reached 80% numeric parsing, or numeric columns were whole-number-only/ID-like.

**Rows missing from both DataFrames and the report**
Check **SKIPPED LINES**: rows with the wrong number of delimiters are dropped at load time.

**Report file name isn't what I expected**
The name is the longest common part of the two input file names; if they share nothing meaningful, it's `reconciliation_recon_report.csv`.

**Need a non-zero exit code on mismatch (CI use)**
The script always exits 0 on success. Add a check after `reconcile()` (e.g. `sys.exit(1)` if `mismatched_records`, `unresolved_legacy_records` or `new_only_records` is non-zero), or parse the report.
