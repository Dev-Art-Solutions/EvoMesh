import sys

sys.path.insert(0, "src")
from evomesh.codebase import survey

mods = {m.name: m for m in survey(".")}
print("TOTAL MODULES:", len(mods))
print("\n=== DEAD MODULES REACHABILITY ===")
dead_mods = [
    "_agent_ids", "agent_label", "cycles", "mesh",
    "mesh_utils", "node", "phase_label", "verdict_label",
]
for mod in dead_mods:
    m = mods.get(mod)
    if m:
        print(f"{mod}: reachable={m.reachable} imported_by={m.imported_by}")
    else:
        print(f"{mod}: NOT IN PROJECT")
