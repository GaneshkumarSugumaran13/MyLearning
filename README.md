# CSV Reconciliation Sample Package

## Included files

- `recon.py` — updated reconciliation script.
- `legacy_sample_30.csv` — sample legacy input with 30 data rows.
- `new_sample_30.csv` — sample new/current input with 30 data rows.
- `expected_reconciliation_report.csv` — expected unified CSV report for the two sample inputs.

## Change made

The unified CSV report now uses **two empty CSV records (two blank lines)** whenever the report calls for visual separation.

This applies to:
- major report sections such as Encoding, Schema Mismatches, Count Mismatch, Newline/EOL, Mismatched Data, and Numeric Rollup Validation;
- each individual mismatched error-type block under `MISMATCHED DATA`;
- file-level and grouped rollup subsections.

The change is implemented centrally in `export_unified_csv()` through the `blank()` helper, so newly added sections that use the same helper automatically receive the same spacing.

## What the sample data demonstrates

The 30-row samples intentionally exercise several existing reconciliation features:

1. Rows are shuffled in the new file, demonstrating that row order is not used as the match key.
2. Multiple distinct error types exist in the same column (`amount`), so the report shows multiple error-set blocks.
3. Additional mismatches exist in `category`, `quantity`, `status`, and `notes/remarks`.
4. The new file uses `remarks` instead of `notes` to demonstrate positional schema reporting.
5. One legacy row is intentionally unresolved and one new-only row is produced while both files still contain 30 records.
6. Numeric rollup validation is exercised through the `amount` column.
7. Grouped rollups are exercised through `region`, `category`, and `status`.
8. Both sample files are UTF-8/ASCII-compatible CSVs, allowing the script's automatic encoding detection to run without explicit encoding arguments.

## Requirements

Python 3.10+ is recommended.

Install dependencies:

```bash
pip install pandas chardet
```

## Run the sample

From the folder containing the files:

```bash
python recon.py legacy_sample_30.csv new_sample_30.csv --output reconciliation_output
```

The unified report will be created at:

```text
reconciliation_output/reconciliation_report.csv
```

## Expected result

The supplied `expected_reconciliation_report.csv` is the expected unified report for the supplied 30-row inputs.

Key expected summary values:

- Legacy records: 30
- New records: 30
- Exact raw matches: 22
- Mismatched records: 7
- Unresolved legacy records: 1
- New-only records: 1
- Schema difference: position 8, `notes` vs `remarks`
- Numeric column identified: `amount`
- Grouping columns identified: `region`, `category`, `status`

## Important note about the CSV spacing

The blank lines are intentionally present in the expected report. Open the CSV as a text/CSV file rather than relying only on a spreadsheet preview, because spreadsheet applications may visually collapse or otherwise render blank CSV records differently.

The requested spacing is represented by **two consecutive empty CSV records before/after applicable sections and between error-set blocks**.
