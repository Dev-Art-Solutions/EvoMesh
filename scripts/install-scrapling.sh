#!/usr/bin/env sh
# Provisions Scrapling (https://github.com/D4Vinci/Scrapling) in its own venv,
# never in .venv. Scrapling's fetchers extra alone pulls in a dozen packages --
# a browser automation stack among them -- and CLAUDE.md rule 16 keeps this
# project's own runtime dependencies at five. The Web.Fetch skill shells out to
# whatever this script builds; it never imports scrapling directly.
#
# --with-browser pulls down Chromium via Playwright -- hundreds of MB, one
# time -- so the fetch tool's dynamic=true (a real browser, for JavaScript-
# rendered pages) works. Off unless asked for: the static fetcher alone
# already handles most pages. Re-run with it later; it is additive.
set -eu

root=""
with_browser=0
for arg in "$@"; do
    if [ "$arg" = "--with-browser" ]; then
        with_browser=1
    else
        root="$arg"
    fi
done
root="${root:-$(cd "$(dirname "$0")/.." && pwd)}"
venv_path="$root/.runtime/scrapling"

uv venv "$venv_path" --python 3.12
uv pip install --python "$venv_path/bin/python" "scrapling[rag]"

executable="$venv_path/bin/scrapling"

if [ "$with_browser" = "1" ]; then
    "$executable" install
fi

echo ""
echo "Scrapling installed at $executable"
echo "Set in evomesh.yaml:"
echo "  scraping:"
echo "    enabled: true"
echo "    executable: '$executable'"
echo ""
if [ "$with_browser" = "1" ]; then
    echo "Browser installed: dynamic=true on the fetch tool works too."
else
    echo "This installed the static fetcher only (curl_cffi -- no browser download)."
    echo "The fetch tool's dynamic=true needs a real browser; re-run with --with-browser"
    echo "to add it (hundreds of MB, one time), or by hand from this venv:"
    echo "  '$executable' install"
fi
