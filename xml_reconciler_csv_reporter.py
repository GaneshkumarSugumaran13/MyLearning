import xml.etree.ElementTree as ET
import re
import csv
from collections import defaultdict
from typing import List, Dict, Any, Tuple, Optional

class XMLReconcilerCSVReporter:
    def __init__(
        self, 
        key_fields: Optional[List[str]] = None, 
        normalize_leading_decimal_zeros: bool = True
    ):
        self.key_fields = key_fields or ['id', 'ID', 'AccountCode', 'Element1', 'code', 'Code']
        self.normalize_leading_decimal_zeros = normalize_leading_decimal_zeros

    def inspect_and_read_file(self, file_path: str) -> Tuple[str, str, Optional[str], Optional[ET.Element]]:
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()

        # Encoding Inspection
        if raw_bytes.startswith(b'\xef\xbb\xbf'):
            encoding = "UTF-8-BOM"
            decoded_text = raw_bytes.decode('utf-8-sig')
        else:
            try:
                decoded_text = raw_bytes.decode('utf-8')
                encoding = "UTF-8"
            except UnicodeDecodeError:
                decoded_text = raw_bytes.decode('cp1252')
                encoding = "CP1252"

        # Line Ending Inspection
        if "\r\n" in decoded_text:
            line_ending = "CRLF (Windows)"
        elif "\r" in decoded_text and "\n" not in decoded_text:
            line_ending = "CR (Classic Mac)"
        else:
            line_ending = "LF (Unix/Linux)"

        # Well-formedness Check
        try:
            root = ET.fromstring(decoded_text)
            parse_error = None
        except ET.ParseError as e:
            root = None
            parse_error = str(e)

        return encoding, line_ending, parse_error, root

    def normalize_value(self, val: Optional[str]) -> str:
        if val is None:
            return ""
        s = val.strip()

        # Decimal leading zero normalization (.00 -> 0.00, -.5 -> -0.5)
        if self.normalize_leading_decimal_zeros:
            if re.match(r'^\.\d+$', s):
                s = '0' + s
            elif re.match(r'^-\.\d+$', s):
                s = '-0' + s[2:]

        return s

    def _extract_record_key(self, elem: ET.Element, fallback_idx: int) -> str:
        key_parts = []
        for attr, value in sorted(elem.attrib.items()):
            if attr in self.key_fields:
                key_parts.append(f"@{attr}={self.normalize_value(value)}")

        for child in elem:
            if child.tag in self.key_fields and child.text is not None:
                key_parts.append(f"{child.tag}={self.normalize_value(child.text)}")

        if key_parts:
            return ":".join(key_parts)

        if not list(elem):
            val = self.normalize_value(elem.text)
            return f"{elem.tag}={val}" if val else f"{elem.tag}_idx_{fallback_idx}"

        return f"index_{fallback_idx}"

    def reconcile(self, legacy_path: str, new_path: str, output_csv: str = "recon.csv"):
        leg_enc, leg_le, leg_err, leg_root = self.inspect_and_read_file(legacy_path)
        new_enc, new_le, new_err, new_root = self.inspect_and_read_file(new_path)

        metadata_diffs = []
        malformed_errors = []
        discrepancies = []

        # File-Level Checks
        if leg_enc != new_enc:
            metadata_diffs.append({"type": "ENCODING_MISMATCH", "legacy": leg_enc, "new": new_enc})
        if leg_le != new_le:
            metadata_diffs.append({"type": "LINE_ENDING_MISMATCH", "legacy": leg_le, "new": new_le})

        if leg_err:
            malformed_errors.append({"file": "Legacy", "error": leg_err})
        if new_err:
            malformed_errors.append({"file": "New", "error": new_err})

        total_leg_records = 0
        total_new_records = 0
        matched_records = 0
        unmatched_leg = 0
        unmatched_new = 0

        if leg_root is not None and new_root is not None:
            # Perform Node Comparison
            stats = self._compare_nodes(leg_root, new_root, xpath=leg_root.tag, discrepancies=discrepancies)
            total_leg_records = stats['leg_count']
            total_new_records = stats['new_count']
            matched_records = stats['matched_count']
            unmatched_leg = stats['unmatched_leg']
            unmatched_new = stats['unmatched_new']

        # Generate Structured recon.csv
        self._write_recon_csv(
            output_csv=output_csv,
            leg_enc=leg_enc, new_enc=new_enc,
            leg_le=leg_le, new_le=new_le,
            leg_err=leg_err, new_err=new_err,
            total_leg=total_leg_records, total_new=total_new_records,
            matched_cnt=matched_records,
            unmatched_leg=unmatched_leg, unmatched_new=unmatched_new,
            metadata_diffs=metadata_diffs,
            discrepancies=discrepancies
        )

    def _compare_nodes(self, elem1: ET.Element, elem2: ET.Element, xpath: str, discrepancies: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {'leg_count': 0, 'new_count': 0, 'matched_count': 0, 'unmatched_leg': 0, 'unmatched_new': 0}

        if elem1.tag != elem2.tag:
            discrepancies.append({
                "record_key": "ROOT", "xpath": xpath, "column_tag": elem1.tag,
                "issue_type": "TAG_MISMATCH", "legacy": elem1.tag, "new": elem2.tag,
                "description": f"Root tag mismatch: {elem1.tag} vs {elem2.tag}"
            })
            return stats

        # Attributes
        attr1, attr2 = elem1.attrib, elem2.attrib
        for attr in sorted(set(attr1.keys()).union(set(attr2.keys()))):
            attr_xpath = f"{xpath}/@{attr}"
            if attr not in attr1:
                discrepancies.append({
                    "record_key": "ATTRIBUTE", "xpath": attr_xpath, "column_tag": f"@{attr}",
                    "issue_type": "EXTRA_ATTRIBUTE", "legacy": None, "new": attr2[attr],
                    "description": f"Attribute '@{attr}' added in New XML"
                })
            elif attr not in attr2:
                discrepancies.append({
                    "record_key": "ATTRIBUTE", "xpath": attr_xpath, "column_tag": f"@{attr}",
                    "issue_type": "MISSING_ATTRIBUTE", "legacy": attr1[attr], "new": None,
                    "description": f"Attribute '@{attr}' missing in New XML"
                })
            else:
                v1, v2 = self.normalize_value(attr1[attr]), self.normalize_value(attr2[attr])
                if v1 != v2:
                    discrepancies.append({
                        "record_key": "ATTRIBUTE", "xpath": attr_xpath, "column_tag": f"@{attr}",
                        "issue_type": "ATTRIBUTE_VALUE_MISMATCH", "legacy": attr1[attr], "new": attr2[attr],
                        "description": f"Value mismatch in attribute '@{attr}'"
                    })

        # Leaf Text
        children1, children2 = list(elem1), list(elem2)
        if not children1 and not children2:
            t1 = self.normalize_value(elem1.text)
            t2 = self.normalize_value(elem2.text)
            if t1 != t2:
                col_name = xpath.split('/')[-1].split('[')[0]
                discrepancies.append({
                    "record_key": "LEAF_TEXT", "xpath": xpath, "column_tag": col_name,
                    "issue_type": "TEXT_VALUE_MISMATCH", "legacy": elem1.text, "new": elem2.text,
                    "description": f"Text value mismatch in <{col_name}>"
                })

        # Children Grouping
        g1, g2 = self._group_children(children1), self._group_children(children2)
        for tag in sorted(set(g1.keys()).union(set(g2.keys()))):
            list1, list2 = g1.get(tag, []), g2.get(tag, [])

            if tag == "Record":
                stats['leg_count'] += len(list1)
                stats['new_count'] += len(list2)

            keyed1 = {self._extract_record_key(c, idx): c for idx, c in enumerate(list1)}
            keyed2 = {self._extract_record_key(c, idx): c for idx, c in enumerate(list2)}

            all_keys = sorted(set(keyed1.keys()).union(set(keyed2.keys())))

            for key in all_keys:
                child_xpath = f"{xpath}/{tag}[key='{key}']"
                if key not in keyed1:
                    if tag == "Record": stats['unmatched_new'] += 1
                    discrepancies.append({
                        "record_key": key, "xpath": child_xpath, "column_tag": tag,
                        "issue_type": "UNMATCHED_NEW_RECORD", "legacy": None, "new": key,
                        "description": f"Record '{key}' present in New XML but missing in Legacy XML"
                    })
                elif key not in keyed2:
                    if tag == "Record": stats['unmatched_leg'] += 1
                    discrepancies.append({
                        "record_key": key, "xpath": child_xpath, "column_tag": tag,
                        "issue_type": "UNMATCHED_LEGACY_RECORD", "legacy": key, "new": None,
                        "description": f"Record '{key}' present in Legacy XML but missing in New XML"
                    })
                else:
                    if tag == "Record": stats['matched_count'] += 1
                    sub_stats = self._compare_nodes(keyed1[key], keyed2[key], child_xpath, discrepancies)
                    stats['leg_count'] += sub_stats['leg_count']
                    stats['new_count'] += sub_stats['new_count']

        return stats

    def _group_children(self, children: List[ET.Element]) -> Dict[str, List[ET.Element]]:
        grouped = {}
        for child in children:
            grouped.setdefault(child.tag, []).append(child)
        return grouped

    def _write_recon_csv(
        self, output_csv: str, leg_enc: str, new_enc: str, leg_le: str, new_le: str,
        leg_err: Optional[str], new_err: Optional[str], total_leg: int, total_new: int,
        matched_cnt: int, unmatched_leg: int, unmatched_new: int,
        metadata_diffs: List[Dict[str, Any]], discrepancies: List[Dict[str, Any]]
    ):
        with open(output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)

            # ---------------------------------------------------------------
            # SECTION 1: FILE-LEVEL METADATA & ROW COUNT SUMMARY
            # ---------------------------------------------------------------
            writer.writerow(["=== SECTION 1: FILE METADATA & SUMMARY METRICS ==="])
            writer.writerow(["Metric Category", "Legacy File", "New File", "Status / Difference"])
            writer.writerow(["File Encoding", leg_enc, new_enc, "MATCH" if leg_enc == new_enc else "MISMATCH"])
            writer.writerow(["Line Endings", leg_le, new_le, "MATCH" if leg_le == new_le else "MISMATCH"])
            writer.writerow(["Malformed Syntax Check", leg_err or "PASSED", new_err or "PASSED", "PASSED" if not (leg_err or new_err) else "ERROR"])
            writer.writerow(["Total Records Count", total_leg, total_new, f"Delta: {abs(total_leg - total_new)}"])
            writer.writerow(["Matched Records Count", matched_cnt, matched_cnt, "Aligned by key"])
            writer.writerow(["Unmatched Legacy Records", unmatched_leg, 0, f"{unmatched_leg} Missing in New"])
            writer.writerow(["Unmatched New Records", 0, unmatched_new, f"{unmatched_new} Extra in New"])
            writer.writerow(["Total Discrepancies Found", len(discrepancies), len(discrepancies), f"{len(discrepancies)} Total Issues"])
            writer.writerow([])  # Blank spacer row

            # ---------------------------------------------------------------
            # SECTION 2: ISSUE TYPES SUMMARY & REPRESENTATIVE EXAMPLES
            # ---------------------------------------------------------------
            writer.writerow(["=== SECTION 2: ISSUE TYPE SUMMARY & EXAMPLES BY COLUMN ==="])
            writer.writerow(["Issue Type", "Target Column / Tag", "Total Occurrences", "Example Legacy Value", "Example New Value", "Description Example"])

            # Group discrepancies by Issue Type + Column Tag
            grouped_issues = defaultdict(list)
            for d in discrepancies:
                grouped_issues[(d['issue_type'], d['column_tag'])].append(d)

            for (issue_type, col_tag), items in sorted(grouped_issues.items()):
                sample = items[0]
                writer.writerow([
                    issue_type,
                    col_tag,
                    len(items),
                    str(sample['legacy']) if sample['legacy'] is not None else "<MISSING>",
                    str(sample['new']) if sample['new'] is not None else "<MISSING>",
                    sample['description']
                ])

            writer.writerow([])  # Blank spacer row

            # ---------------------------------------------------------------
            # SECTION 3: DETAILED ITEMIZED DISCREPANCY LOG
            # ---------------------------------------------------------------
            writer.writerow(["=== SECTION 3: DETAILED DISCREPANCY LOG ==="])
            writer.writerow(["Record Key", "Target Column / Tag", "Issue Type", "Legacy Value", "New Value", "XPath", "Description"])

            for d in discrepancies:
                writer.writerow([
                    d['record_key'],
                    d['column_tag'],
                    d['issue_type'],
                    str(d['legacy']) if d['legacy'] is not None else "<MISSING>",
                    str(d['new']) if d['new'] is not None else "<MISSING>",
                    d['xpath'],
                    d['description']
                ])

        print(f"Reconciliation completed successfully. Report saved to '{output_csv}'.")


if __name__ == "__main__":
    reconciler = XMLReconcilerCSVReporter(
        key_fields=["id", "AccountCode"],
        normalize_leading_decimal_zeros=True
    )
    reconciler.reconcile("legacy_big.xml", "new_big.xml", output_csv="recon.csv")