#!/usr/bin/env sh
# Provisions Scrapling (https://github.com/D4Vinci/Scrapling) in its own venv,
# never in .venv. Scrapling's fetchers extra alone pulls in a dozen packages --
# a browser automation stack among them -- and CLAUDE.md rule 16 keeps this
# project's own runtime dependencies at five. The Web.Fetch skill shells out to
# whatever this script builds; it never imports scrapling directly.
set -eu

root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
venv_path="$root/.runtime/scrapling"

uv venv "$venv_path" --python 3.12
uv pip install --python "$venv_path/bin/python" "scrapling[rag]"

executable="$venv_path/bin/scrapling"
echo ""
echo "Scrapling installed at $executable"
echo "Set in evomesh.yaml:"
echo "  scraping:"
echo "    enabled: true"
echo "    executable: '$executable'"
echo ""
echo "This installs the static fetcher only (curl_cffi -- no browser download)."
echo "Web.Fetch works with that alone. For JS-rendered pages, a real browser"
echo "is needed too; run once more, from this venv:"
echo "  '$executable' install"
