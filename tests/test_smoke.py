"""The smoke check candidate validation runs last (`python -m evomesh.smoke`).

Found live 2026-09-26: it still expected four system agents after the Idea
Scout made five. Nothing in the suite ran it, so CI stayed green while every
candidate failed validation -- and the failure was then hidden behind a
PermissionError from the temporary directory's cleanup, which validation reads
as the host blocking the run rather than the candidate failing."""

from __future__ import annotations

from evomesh.smoke import smoke


async def test_the_smoke_check_passes_on_this_tree() -> None:
    await smoke()
