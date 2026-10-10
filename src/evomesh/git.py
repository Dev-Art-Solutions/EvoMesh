from __future__ import annotations

import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from evomesh.processes import run_command

logger = logging.getLogger(__name__)

# Who signs a generation. The mesh authors its own commits, and a human reading
# the history has to be able to tell them apart from their own work at a glance,
# so the identity is the agent's rather than whatever git.config happens to hold.
DEFAULT_AUTHOR_NAME = "Mesh Evo Agent"
DEFAULT_AUTHOR_EMAIL = "mesh-evo-agent@evomesh.local"


class GitError(RuntimeError):
    pass


LOCK_IN_USE = re.compile(r"Unable to create '([^']+\.lock)': File exists")
# Long enough that no git process still owns it: a live writer fills the lock
# with the new index within milliseconds, an editor-held `git commit` included.
STALE_LOCK_SECONDS = 60.0


def release_stale_index_lock(error: str, *, now: float | None = None) -> bool:
    """Remove the lock a git error names, only when it is plainly abandoned.

    Found live 2026-10-10: an empty .git/index.lock from a git process that
    died mid-command sat in the checkout for minutes, and every commit after
    it failed -- a promotion and an approved idea alike would have, until a
    human noticed. Empty and a minute old is the line: anything else may be
    a git process that is still working, and is left to it.
    """
    match = LOCK_IN_USE.search(error)
    if match is None:
        return False
    lock = Path(match.group(1))
    try:
        stat = lock.stat()
    except OSError:
        return False
    current = time.time() if now is None else now
    if stat.st_size != 0 or current - stat.st_mtime < STALE_LOCK_SECONDS:
        return False
    try:
        lock.unlink()
    except OSError:
        return False
    logger.warning("removed a stale, empty git lock left by a dead process: %s", lock)
    return True


@dataclass(frozen=True)
class GitIdentity:
    name: str = DEFAULT_AUTHOR_NAME
    email: str = DEFAULT_AUTHOR_EMAIL

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass(frozen=True)
class PublishPolicy:
    """Where a landed generation goes after it is committed.

    ``branch`` empty means "whatever the checkout is on", which is the only
    answer that stays right when a human moves the mesh onto a working branch.
    """

    enabled: bool = True
    remote: str = "origin"
    branch: str = ""


@dataclass
class GitRepository:
    root: Path
    identity: GitIdentity = field(default_factory=GitIdentity)

    async def run(self, *arguments: str) -> str:
        try:
            return await self._run(*arguments)
        except GitError as exc:
            if not release_stale_index_lock(str(exc)):
                raise
        return await self._run(*arguments)

    async def _run(self, *arguments: str) -> str:
        result = await run_command(
            "git",
            "-C",
            str(self.root),
            # Passed per invocation rather than written into .git/config: the
            # mesh never edits a human's checkout configuration, and a commit it
            # authors is signed by the agent even on a machine that has no
            # user.name set at all.
            "-c",
            f"user.name={self.identity.name}",
            "-c",
            f"user.email={self.identity.email}",
            *arguments,
        )
        if result.exit_code != 0:
            raise GitError(result.output.strip())
        return result.output

    async def status(self) -> str:
        return await self.run("status", "--short")

    async def diff(self) -> str:
        return await self.run("diff", "--")

    async def current_commit(self) -> str:
        return (await self.run("rev-parse", "HEAD")).strip()

    async def current_branch(self) -> str:
        """The checked-out branch, or an empty string on a detached HEAD."""
        name = (await self.run("rev-parse", "--abbrev-ref", "HEAD")).strip()
        return "" if name == "HEAD" else name

    async def remotes(self) -> list[str]:
        return [line.strip() for line in (await self.run("remote")).splitlines() if line.strip()]

    async def create_branch(self, name: str) -> None:
        await self.run("switch", "-c", name)

    async def commit_mutation(self, generation: int, objective: str) -> str:
        await self.run("add", "-A")
        message = (
            f"evolve(environment): candidate generation {generation}\n\n"
            f"Objective:\n{objective}\n\nCreated by:\n{self.identity}"
        )
        await self.run("commit", "-m", message)
        return await self.current_commit()

    async def is_clean(self) -> bool:
        return not (await self.status()).strip()

    async def push(self, remote: str = "origin", branch: str = "") -> str:
        """Publish the current branch, and say plainly why when it cannot.

        Every refusal here is a configuration fact a human can act on -- no
        remote, a detached HEAD, no credentials -- so each one is raised as a
        GitError carrying the reason rather than swallowed into a bare False.
        """
        target = branch or await self.current_branch()
        if not target:
            raise GitError(
                "HEAD is detached, so there is no branch to publish; check out a branch"
            )
        configured = await self.remotes()
        if remote not in configured:
            known = ", ".join(configured) or "none"
            raise GitError(f"remote '{remote}' is not configured (remotes: {known})")
        # The explicit refspec keeps the push honest when the local branch and
        # the remote branch are named differently on a human's checkout.
        return await self.run("push", "--set-upstream", remote, f"{target}:{target}")

    async def cherry_pick(self, commit: str) -> str:
        """Land one commit from a candidate worktree on this checkout."""
        try:
            await self.run("cherry-pick", commit)
        except GitError as exc:
            # Never leave a checkout sitting mid-pick. A half-applied generation
            # is worse than a refused one, and the next pass would inherit it.
            with suppress(GitError):
                await self.run("cherry-pick", "--abort")
            raise GitError(f"{commit[:8]} does not apply cleanly: {exc}") from exc
        return await self.current_commit()

    async def reset_to(self, commit: str) -> str:
        await self.run("reset", "--hard", commit)
        return await self.current_commit()

    async def history(self, limit: int = 20) -> str:
        return await self.run("log", f"-{limit}", "--oneline", "--decorate")

    async def tag_generation(self, generation: int) -> None:
        await self.run("tag", f"evomesh-generation-{generation:06d}")
