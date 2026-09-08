import subprocess
import sys

for tool in ("ruff", "pyright"):
    print("=" * 60)
    print(tool)
    print("=" * 60)
    res = subprocess.run(
        [sys.executable, "-m", tool, "src/evomesh/harness_tools.py"],
        capture_output=True,
        text=True,
    )
    print(res.stdout)
    print(res.stderr)

print("=" * 60)
print("pytest")
print("=" * 60)
res = subprocess.run([sys.executable, "-m", "pytest", "-q"], capture_output=True, text=True)
print(res.stdout)
print(res.stderr)
