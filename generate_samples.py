import os

def generate_sample_xml_files():
    legacy_records = []
    new_records = []

    # 1. Matches & Controlled Discrepancies (Rows 1 to 30)
    for i in range(1, 31):
        rec_id = f"R{i:03d}"
        acct_leg = f"00{1000 + i}"
        acct_new = acct_leg
        amt_leg = f"{i * 10}.00"
        amt_new = amt_leg
        status = "ACTIVE"
        curr_leg = "USD"
        curr_new = "USD"

        # Special Edge Case Overrides
        if i == 5:
            # .00 vs 0.00 decimal mismatch (Should PASS / Not Flagged)
            amt_leg = ".00"
            amt_new = "0.00"
        elif i == 10:
            # Integer starting zero mismatch (001010 vs 1010 -> Should FLAG)
            acct_new = f"{1000 + i}"
        elif i == 15:
            # Attribute Value Mismatch
            curr_new = "EUR"
        elif i == 20:
            # Text Value Mismatch
            status = "PENDING"

        legacy_records.append(
            f'        <Record id="{rec_id}">\n'
            f'            <AccountCode>{acct_leg}</AccountCode>\n'
            f'            <Amount currency="{curr_leg}">{amt_leg}</Amount>\n'
            f'            <Status>ACTIVE</Status>\n'
            f'        </Record>'
        )

        new_records.append(
            f'        <Record id="{rec_id}">\n'
            f'            <AccountCode>{acct_new}</AccountCode>\n'
            f'            <Amount currency="{curr_new}">{amt_new}</Amount>\n'
            f'            <Status>{status}</Status>\n'
            f'        </Record>'
        )

    # 2. Jumbled Sequence (Rows 31 to 40 - Shuffled order in New XML)
    jumbled_indices = [35, 31, 39, 32, 40, 33, 37, 34, 38, 36]
    for i in range(31, 41):
        rec_id = f"R{i:03d}"
        legacy_records.append(
            f'        <Record id="{rec_id}">\n'
            f'            <AccountCode>00{2000 + i}</AccountCode>\n'
            f'            <Amount currency="USD">{i * 15}.50</Amount>\n'
            f'            <Status>COMPLETED</Status>\n'
            f'        </Record>'
        )

    for i in jumbled_indices:
        rec_id = f"R{i:03d}"
        new_records.append(
            f'        <Record id="{rec_id}">\n'
            f'            <AccountCode>00{2000 + i}</AccountCode>\n'
            f'            <Amount currency="USD">{i * 15}.50</Amount>\n'
            f'            <Status>COMPLETED</Status>\n'
            f'        </Record>'
        )

    # 3. Unmatched Legacy Records (Rows 41 to 45 -> Missing in New)
    for i in range(41, 46):
        rec_id = f"R{i:03d}"
        legacy_records.append(
            f'        <Record id="{rec_id}">\n'
            f'            <AccountCode>00{3000 + i}</AccountCode>\n'
            f'            <Amount currency="USD">500.00</Amount>\n'
            f'            <Status>LEGACY_ONLY</Status>\n'
            f'        </Record>'
        )

    # 4. Unmatched New Records (Rows 46 to 50 -> Extra in New)
    for i in range(46, 51):
        rec_id = f"R{i:03d}"
        new_records.append(
            f'        <Record id="{rec_id}">\n'
            f'            <AccountCode>00{4000 + i}</AccountCode>\n'
            f'            <Amount currency="USD">750.00</Amount>\n'
            f'            <Status>NEW_ONLY</Status>\n'
            f'        </Record>'
        )

    # File Constructions
    leg_xml_str = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Values variable="lnk" source="string" file_status="ACTIVE">\n'
        '    <HeaderString>\n' +
        "\n".join(legacy_records) + "\n"
        '    </HeaderString>\n'
        '</Values>\n'
    )

    new_xml_str = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        '<Values variable="lnk" source="string" env="PROD">\r\n'
        '    <HeaderString>\r\n' +
        "\r\n".join(new_records) + "\r\n"
        '    </HeaderString>\r\n'
        '</Values>\r\n'
    )

    # Write Legacy: UTF-8 with BOM and Unix LF
    with open("legacy_big.xml", "wb") as f:
        f.write(leg_xml_str.encode("utf-8-sig"))

    # Write New: CP1252 and Windows CRLF
    with open("new_big.xml", "wb") as f:
        f.write(new_xml_str.encode("cp1252"))

    print("Created 'legacy_big.xml' and 'new_big.xml' (50 records each).")

if __name__ == "__main__":
    generate_sample_xml_files()