import sys

sys.path.insert(0, "src")
from pathlib import Path

from evomesh import codebase

root = Path(".")
mods = {m.name: m for m in codebase.survey(root)}
for name in sorted(mods):
    m = mods[name]
    orphan = m.is_orphan
    ep = m.is_entry_point
    print(
        f"{name:24} orphan={orphan!s:5} entry={ep!s:5} "
        f"imported_by={sorted(m.imported_by)} "
        f"imports={sorted(m.imports)}"
    )
