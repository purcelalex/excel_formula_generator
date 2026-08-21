from engine import FormulaEngine

eng = FormulaEngine()

queries = [
    "sum values in column B only where column A equals a city",
    "adună valorile din coloana B dacă coloana A este Chișinău",
    "numără câte celule din coloana B sunt mai mari decât 100",
    "look up a price by product name, return column C from column A",
    "google sheets query all rows where status is active",
    "remove extra spaces from a text column",
    "difference between two dates in years, age from birthdate",
    "cauta pretul dupa nume in coloana A si returneaza coloana B",
    "return one value if a score is 50 or more otherwise fail",
    "list of distinct values from a column",
]

for q in queries:
    out = eng.generate(q, top_n=2)
    print("─" * 72)
    print(f"Q [{out['language']}/{out['platform']}/{out['locale']}]: {q}")
    if out["status"] == "no_match":
        print("  (no match)")
        continue
    tag = "FILLED " if out["status"] == "filled" else "TEMPLATE"
    print(f"  → [{tag}] {out['formula']}   (best={out['best_function']}, conf={out['confidence']})")
    alts = ", ".join(c["name"] for c in out["candidates"][1:])
    if alts:
        print(f"    alternatives: {alts}")
