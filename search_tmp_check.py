import os
import sys

sys.path.insert(0, "src")
from evomesh.humanize import humanize_bytes, humanize_duration

print(humanize_bytes(1536), humanize_duration(123.4))
import evomesh.console  # noqa

print("console imports OK")
os.remove(__file__)
