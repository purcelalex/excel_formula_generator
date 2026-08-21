from table_parser import parse_table
from formula_builder import TableAwareBuilder
import json

builder = TableAwareBuilder()

# A normal Excel/Sheets copy-paste is TAB-separated. \t used here explicitly.
table_tsv = "City\tProduct\tPrice\tStatus\n" \
            "Chișinău\tLaptop\t1200\tActive\n" \
            "București\tMouse\t45\tInactive\n" \
            "Chișinău\tMonitor\t300\tActive\n" \
            "Iași\tKeyboard\t80\tActive"

cases = [
    ("EN, conditional sum", "sum the price where city is Chișinău", table_tsv),
    ("RO, conditional sum",  "adună prețul unde orașul este Chișinău", table_tsv),
    ("EN, count condition",  "count how many rows have status Active", table_tsv),
    ("EN, lookup",           "look up the price by product name", table_tsv),
    ("RO, average",          "media prețului pentru statusul Active", table_tsv),
    ("EN, plain sum",        "sum all the prices", table_tsv),
]

for label, desc, tbl in cases:
    parsed = parse_table(tbl)
    out = builder.build(desc, parsed)
    print("─" * 74)
    print(f"{label}\n  request: {desc}")
    print(f"  columns: {[(c['letter'], c['header'], c['type']) for c in out['columns']]}")
    print(f"  → [{out['status']}] {out['formula']}")
    print(f"    why: {out['rationale']}")

# --- security: a malicious paste with a formula-injection cell ---
print("═" * 74)
print("SECURITY TEST — pasted table containing an injection cell:")
malicious = "Name\tNote\n" \
            "Alice\tOK\n" \
            "Bob\t=cmd|'/c calc'!A1"
parsed = parse_table(malicious)
out = builder.build("count the names", parsed)
print("  defanged cells:", out["safety"]["injection_cells_defanged"])
print("  the dangerous cell is now stored as literal text, not a formula.")
print("  formula returned:", out["formula"])
