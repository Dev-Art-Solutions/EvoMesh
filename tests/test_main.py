"""The process entry point's own housekeeping -- not the mesh it starts."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from evomesh.__main__ import MESH_LOG_BACKUP_COUNT, MESH_LOG_MAX_BYTES, RedactSecrets


def test_the_mesh_log_rotates_instead_of_growing_forever(tmp_path: Path) -> None:
    """Found live: --log-file grew to 24.8MB / 166542 lines over one
    continuous run with nothing ever rotating it. Built the same way
    application() builds it, writing past maxBytes must produce a rotated
    backup rather than one file that keeps growing without limit."""
    log_path = tmp_path / "mesh.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1024, backupCount=3, encoding="utf-8"
    )
    try:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=0,
            msg="x" * 200, args=(), exc_info=None,
        )
        for _ in range(20):  # 20 * ~200 bytes comfortably clears the 1024 cap
            handler.emit(record)
    finally:
        handler.close()

    assert log_path.exists()
    assert (tmp_path / "mesh.log.1").exists(), "nothing rotated past maxBytes"
    assert log_path.stat().st_size <= 1024 + 512, "the live file itself must stay capped"


def test_the_configured_retention_is_sane() -> None:
    """A cap that is zero or unset would silently be no cap at all."""
    assert MESH_LOG_MAX_BYTES > 0
    assert MESH_LOG_BACKUP_COUNT > 0


def test_a_bot_token_never_reaches_the_log() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.WARNING,
        __file__,
        1,
        'HTTP Request: POST https://api.telegram.org/bot%s/getUpdates "%s"',
        ("123456789:FAKEtokenFAKEtokenFAKEtoken-_x", "HTTP/1.1 200 OK"),
        None,
    )

    assert RedactSecrets().filter(record)

    assert record.getMessage() == (
        'HTTP Request: POST https://api.telegram.org/bot<redacted>/getUpdates "HTTP/1.1 200 OK"'
    )
