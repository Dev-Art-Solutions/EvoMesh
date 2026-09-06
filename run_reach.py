import sys

sys.path.insert(0, "src")
from evomesh.codebase import survey

mods = {m.name: m for m in survey(".")}
print("TOTAL MODULES:", len(mods))
print("\n=== ALL MODULES ===")
for name, m in sorted(mods.items()):
    print(f"{name}: reachable={m.reachable} imported_by={m.imported_by} summary={m.summary!r}")
