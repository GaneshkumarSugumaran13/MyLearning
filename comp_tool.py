import argparse
from decimal import Decimal, InvalidOperation
import sys
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


def normalize_value(val: Any) -> Any:
    """Normalizes cell values: strips whitespace, standardizes floats, handles nulls."""
    if pd.isna(val) or val is None:
        return np.nan

    val_str = str(val).strip()
    if val_str == "" or val_str.lower() in ("nan", "null", "none"):
        return np.nan

    try:
        # Standardize numeric values (e.g., .00 vs 0.00, 23.900000 vs 23.90)
        num = float(Decimal(val_str))
        return round(num, 6)
    except (InvalidOperation, ValueError, TypeError):
        return val_str


def classify_mismatch(v_leg: Any, v_lat: Any) -> str:
    """Classifies the root cause of a discrepancy between two values."""
    if pd.isna(v_leg) and not pd.isna(v_lat):
        return "MISSING_IN_LEGACY"
    if not pd.isna(v_leg) and pd.isna(v_lat):
        return "MISSING_IN_LATEST"

    is_leg_num = isinstance(v_leg, (int, float))
    is_lat_num = isinstance(v_lat, (int, float))

    if is_leg_num and is_lat_num:
        return "NUMERIC_VALUE_MISMATCH"
    if is_leg_num != is_lat_num:
        return "TYPE_MISMATCH"

    s_leg = str(v_leg)
    s_lat = str(v_lat)

    if s_leg.lower() == s_lat.lower():
        return "TEXT_CASE_DIFFERENCE"

    return "TEXT_VALUE_MISMATCH"


def load_dataset(
    file_path: str,
    key_cols: List[str],
    where_clause: Optional[str] = None,
    encoding: str = "utf-8",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Loads CSV as string preservation and produces a normalized comparison copy."""
    try:
        df_raw = pd.read_csv(
            file_path,
            dtype=str,
            skipinitialspace=True,
            keep_default_na=False,
            encoding=encoding,
        )
    except Exception as e:
        sys.exit(f"Error reading {file_path}: {e}")

    df_raw.columns = df_raw.columns.str.strip()

    # Validate key columns
    for key in key_cols:
        if key not in df_raw.columns:
            sys.exit(
                f"Error: Key column '{key}' not found in {file_path}.\nAvailable columns: {list(df_raw.columns)}"
            )

    # Apply WHERE clause if provided
    if where_clause:
        try:
            df_raw = df_raw.query(where_clause).copy()
        except Exception as e:
            sys.exit(
                f"Error applying where-clause '{where_clause}' to {file_path}: {e}"
            )

    # Normalize key values to guarantee alignment
    for k in key_cols:
        df_raw[k] = df_raw[k].astype(str).str.strip()

    # Deduplicate on keys
    if df_raw.duplicated(subset=key_cols).any():
        dupe_count = df_raw.duplicated(subset=key_cols).sum()
        print(
            f"Notice: {dupe_count} duplicate key records found in {file_path}. Keeping first occurrence."
        )
        df_raw = df_raw.drop_duplicates(subset=key_cols, keep="first")

    # Generate normalized copy for logical matching
    df_norm = df_raw.copy()
    for col in df_norm.columns:
        df_norm[col] = df_norm[col].apply(normalize_value)

    return df_raw, df_norm


def run_comparison(
    legacy_path: str,
    latest_path: str,
    key_cols: List[str],
    where_clause: Optional[str] = None,
    max_samples: int = 1,
    export_unresolved: bool = False,
):
    print(f"Loading legacy: {legacy_path}")
    df_leg_raw, df_leg_norm = load_dataset(legacy_path, key_cols, where_clause)

    print(f"Loading latest: {latest_path}")
    df_lat_raw, df_lat_norm = load_dataset(latest_path, key_cols, where_clause)

    # 1. Schema / Column Order Check
    leg_cols = list(df_leg_raw.columns)
    lat_cols = list(df_lat_raw.columns)

    if leg_cols != lat_cols:
        print("\n" + "!" * 50)
        print("SCHEMA / COLUMN WARNING")
        print("!" * 50)
        missing_in_lat = set(leg_cols) - set(lat_cols)
        added_in_lat = set(lat_cols) - set(leg_cols)
        if missing_in_lat:
            print(f"- Columns in legacy missing in latest: {missing_in_lat}")
        if added_in_lat:
            print(f"- Columns in latest missing in legacy: {added_in_lat}")
        if not missing_in_lat and not added_in_lat:
            print("- Column names match, but order differs.")
            print(f"  Legacy order: {leg_cols}")
            print(f"  Latest order: {lat_cols}")

    # Set indices to key columns
    df_leg_raw = df_leg_raw.set_index(key_cols)
    df_leg_norm = df_leg_norm.set_index(key_cols)
    df_lat_raw = df_lat_raw.set_index(key_cols)
    df_lat_norm = df_lat_norm.set_index(key_cols)

    # 2. Identify Unresolved / Orphan Records
    leg_keys = set(df_leg_norm.index)
    lat_keys = set(df_lat_norm.index)

    unresolved_leg_keys = list(leg_keys - lat_keys)
    unresolved_lat_keys = list(lat_keys - leg_keys)
    matched_keys = list(leg_keys.intersection(lat_keys))

    df_unresolved_leg = df_leg_raw.loc[unresolved_leg_keys].reset_index()
    df_unresolved_lat = df_lat_raw.loc[unresolved_lat_keys].reset_index()

    # 3. Value-by-Value Comparison on Shared Columns
    common_cols = [c for c in leg_cols if c in lat_cols]
    compare_cols = [c for c in common_cols if c not in key_cols]

    column_errors: Dict[str, Dict[str, List[Tuple]]] = {
        c: {} for c in compare_cols
    }

    for col in compare_cols:
        col_leg = df_leg_norm.loc[matched_keys, col]
        col_lat = df_lat_norm.loc[matched_keys, col]

        both_na = col_leg.isna() & col_lat.isna()
        diff_mask = (col_leg != col_lat) & (~both_na)

        if diff_mask.any():
            diff_keys = col_leg[diff_mask].index
            for k in diff_keys:
                v_leg = col_leg.at[k]
                v_lat = col_lat.at[k]
                err_type = classify_mismatch(v_leg, v_lat)

                if err_type not in column_errors[col]:
                    column_errors[col][err_type] = []
                column_errors[col][err_type].append((k, v_leg, v_lat))

    # ==========================================
    # PRINT RESULTS
    # ==========================================
    print("\n" + "=" * 80)
    print("1. UNRESOLVED / ORPHAN RECORDS (No Key Matches Across Files)")
    print("=" * 80)
    print(f"Total Unresolved in Legacy: {len(df_unresolved_leg)}")
    if not df_unresolved_leg.empty:
        print("Sample (up to 3 rows):")
        print(df_unresolved_leg.head(3).to_string(index=False))

    print(f"\nTotal Unresolved in Latest: {len(df_unresolved_lat)}")
    if not df_unresolved_lat.empty:
        print("Sample (up to 3 rows):")
        print(df_unresolved_lat.head(3).to_string(index=False))

    print("\n" + "=" * 80)
    print("2. COLUMN-WISE DISTINCT ERROR TYPES & PAIRED ROW DUMPS")
    print("=" * 80)

    has_diffs = False
    for col, errors in column_errors.items():
        if not errors:
            continue
        has_diffs = True
        print(f"\n>>> COLUMN: [{col}]")

        for err_type, instances in errors.items():
            print(
                f"  └─ ERROR TYPE: {err_type} (Total Occurrences: {len(instances)})"
            )

            samples = instances[:max_samples]
            for idx, (k, v_leg, v_lat) in enumerate(samples, 1):
                raw_leg_row = df_leg_raw.loc[k].to_dict()
                raw_lat_row = df_lat_raw.loc[k].to_dict()

                # Difference in the first line
                print(
                    f"     [Sample {idx}] DIFF on {col} -> Legacy: '{v_leg}' vs Latest: '{v_lat}'"
                )
                print(f"       Legacy Row: {raw_leg_row}")
                print(f"       Latest Row: {raw_lat_row}")

    if not has_diffs:
        print("No value discrepancies found across matched records.")

    # Export optional CSV dumps for unresolved records
    if export_unresolved:
        if not df_unresolved_leg.empty:
            df_unresolved_leg.to_csv("unresolved_legacy.csv", index=False)
            print("\nSaved unresolved legacy records to: unresolved_legacy.csv")
        if not df_unresolved_lat.empty:
            df_unresolved_lat.to_csv("unresolved_latest.csv", index=False)
            print("Saved unresolved latest records to: unresolved_latest.csv")


def main():
    parser = argparse.ArgumentParser(
        description="Row-by-row and field-by-field CSV comparison tool with value normalization."
    )

    # First two positional arguments are the file paths
    parser.add_argument("legacy_path", help="Path to the legacy CSV file")
    parser.add_argument("latest_path", help="Path to the latest CSV file")

    # Options
    parser.add_argument(
        "--keys",
        "-k",
        nargs="+",
        required=True,
        help="One or more column names to use as primary keys for matching (e.g. --keys ID or -k ID ORG_CODE)",
    )
    parser.add_argument(
        "--where",
        "-w",
        default=None,
        help="Pandas query expression applied to filter both CSVs before comparison (e.g. \"STATUS == 'ACTIVE'\")",
    )
    parser.add_argument(
        "--samples",
        "-s",
        type=int,
        default=1,
        help="Max number of paired row examples to print per error type (default: 1)",
    )
    parser.add_argument(
        "--export-unresolved",
        action="store_true",
        help="Export orphan/unresolved records from both files into separate CSV files",
    )

    args = parser.parse_args()

    run_comparison(
        legacy_path=args.legacy_path,
        latest_path=args.latest_path,
        key_cols=args.keys,
        where_clause=args.where,
        max_samples=args.samples,
        export_unresolved=args.export_unresolved,
    )


if __name__ == "__main__":
    main()