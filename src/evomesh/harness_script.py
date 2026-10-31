"""The small piece of a shell a model actually reaches for, without a shell.

The harness's ``shell`` tool runs one program with no interpreter between it
and the model (harness_tools.tool_shell says why). Models trained on a real
terminal do not write one program, though. Across the last 501 harness
sessions (2026-10-31), 540 calls were refused for an operator and 1100 more
for a program that only appeared because it sat in a pipeline:
``python x.py 2>&1 | head -40``, ``find / -name config.json 2>/dev/null |
grep -v node_modules | head -20``, ``ls -la && echo --- && cat config.json``.
Every one of those cost a step and taught nothing, because the next call had
the same shape.

Codex (github.com/openai/codex, ``shell-command/src/bash.rs``) answers the
same problem by parsing a script into a *word-only command sequence*: plain
words and quoted strings joined by ``&&``, ``||``, ``;`` and ``|``, anything
else (substitution, subshells, redirects) rejected outright. Each command in
the sequence is then judged on its own. This module is that idea, sized for
this harness: the sequence is split here, every command still goes through
the same allow-list and read-only translation a single command does, and the
programs that only ever appear *after* a pipe (head, tail, grep, wc, sort,
uniq, cat) are applied to the text in Python instead of being run -- no new
program is ever started on the model's behalf.

Pure functions only: no ToolContext, no I/O, so harness_tools can import this
without a cycle and the parsing can be tested on strings.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

CONNECTORS = ("&&", "||", ";")
# What a pipe may lead into. Everything here is applied to text in Python;
# anything else after a pipe (awk, sed, xargs, tee, python ...) is refused,
# never run -- a filter that silently passed text through would be a wrong
# answer that looks like a right one.
FILTERS = frozenset({"head", "tail", "grep", "egrep", "fgrep", "wc", "sort", "uniq", "cat"})
# At most this many commands per call: a model that writes twenty `echo`s in
# one script is not helped by the harness running all of them.
MAX_COMMANDS = 8
_NULL_TARGETS = frozenset({"/dev/null", "nul", "NUL", "$null"})
_FD_TOKENS = frozenset({"0", "1", "2"})
_STDOUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
_DUP_REDIRECTS = frozenset({">&", "<&"})
_INPUT_REDIRECTS = frozenset({"<", "<<", "<<<", "<<-"})
_HEREDOC = re.compile(
    r"^\s*(?P<program>\S+)(?:\s+-)?\s*<<-?\s*(?P<quote>['\"]?)(?P<tag>[A-Za-z_]\w*)(?P=quote)"
    r"[ \t]*\r?\n(?P<body>.*?)\r?\n[ \t]*(?P=tag)[ \t]*$",
    re.DOTALL,
)


_NO_FILE = "after a pipe, {name} reads the output before it -- drop the file name"


class ScriptRefused(ValueError):
    """The script uses something this module will not imitate."""


@dataclass
class Stage:
    """One command of a pipeline: its words, and what its redirects meant."""

    words: list[str]
    # `>/dev/null` (or `&>/dev/null`): the command runs, its output is dropped.
    discard_output: bool = False


@dataclass
class Command:
    """A pipeline, and the connector that decides whether it runs at all."""

    connector: str
    stages: list[Stage] = field(default_factory=list)

    def shown(self) -> str:
        return " | ".join(shlex.join(stage.words) for stage in self.stages)


def needs_script(parts: list[str]) -> bool:
    """Whether plain ``shlex.split`` left an operator lying in the words.

    Only then is a command parsed as a script. A plain command keeps the
    exact tokens it always had: the script tokenizer also splits unquoted
    parentheses, which ``python -c print(1)`` would not survive.
    """
    return any(
        part in CONNECTORS
        or part in ("|", "&")
        or part.startswith(("<", ">", "2>", "1>", "&>"))
        or part.endswith(";")
        for part in parts[1:]
    ) or parts[0].endswith(";")


def heredoc_command(raw: str) -> list[str] | None:
    """``python3 - <<'EOF' ... EOF`` as ``python -c <body>``, or None.

    Found live: 36 refused calls in 501 sessions, and before the refusal
    existed each one hung for the full shell timeout, because nothing feeds
    the child's stdin. A heredoc into an interpreter is a script passed the
    long way round; passing it as ``-c`` is what the model meant. Only for
    python, and only when nothing follows the closing tag.
    """
    match = _HEREDOC.match(raw)
    if match is None:
        return None
    program = match.group("program")
    if program.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe") not in (
        "python",
        "python3",
        "py",
    ):
        return None
    return [program, "-c", match.group("body")]


class Operator(str):
    """An unquoted `|`, `&&`, `>` ... -- as opposed to the same text quoted,
    which is a word: `grep '|'` searches for a bar."""


_PUNCTUATION = "();<>|&"


def tokens(raw: str) -> list[str]:
    """Words and operators, with POSIX quoting and escapes: ``"a|b"`` and
    ``'|'`` stay words, and only an unquoted run of ``();<>|&`` is an
    Operator.

    Hand-written because neither shlex mode fits. POSIX mode drops the quotes
    and with them the difference between `|` and `'|'` -- found live
    2026-10-31, the first hour on this parser: `... | grep '|' | sort -u` was
    refused as "a pipe with nothing on one side". Non-POSIX mode keeps the
    quotes but not backslash escapes (`\\(`, `\\"`), which broke 22 logged
    scripts that had parsed before. Raises ValueError for an unclosed quote,
    as shlex does.
    """
    found: list[str] = []
    word: list[str] = []
    started = False
    quote = ""
    index = 0

    def flush() -> None:
        nonlocal started
        if started:
            found.append("".join(word))
            word.clear()
            started = False

    while index < len(raw):
        character = raw[index]
        following = raw[index + 1] if index + 1 < len(raw) else ""
        if quote == "'":
            if character == "'":
                quote = ""
            else:
                word.append(character)
        elif quote == '"':
            if character == '"':
                quote = ""
            elif character == "\\" and following in '"\\$`':
                word.append(following)
                index += 1
            else:
                word.append(character)
        elif character == "\\":
            if following == "\n":
                index += 1
            elif following:
                word.append(following)
                started = True
                index += 1
        elif character in "'\"":
            quote = character
            started = True
        elif character.isspace():
            flush()
        elif character in _PUNCTUATION:
            flush()
            end = index
            while end < len(raw) and raw[end] in _PUNCTUATION:
                end += 1
            found.append(Operator(raw[index:end]))
            index = end
            continue
        else:
            if character == "`" or (character == "$" and following == "("):
                raise ScriptRefused(
                    "command substitution (`$(...)` or backticks) is not supported. Use "
                    "one plain command, or several joined by && || ; |."
                )
            word.append(character)
            started = True
        index += 1
    if quote:
        raise ValueError("No closing quotation")
    flush()
    return found


def _stage(words: list[str]) -> Stage:
    """Strip the redirects a model adds out of habit; refuse the rest.

    ``2>&1`` and ``2>/dev/null`` change nothing here (stderr is already folded
    into the output), ``>/dev/null`` drops the output. A redirect into a real
    file is refused: it would change a file behind edit/write's back, the same
    escape PYTHON_ESCAPE_HINT exists for.
    """
    kept: list[str] = []
    discard = False
    index = 0
    while index < len(words):
        word = words[index]
        operator = isinstance(word, Operator)
        redirect = operator and word in _STDOUT_REDIRECTS | _DUP_REDIRECTS | _INPUT_REDIRECTS
        if redirect and kept and kept[-1] in _FD_TOKENS and len(kept) > 1:
            descriptor = kept.pop()
        else:
            descriptor = ""
        if operator and word in _DUP_REDIRECTS:
            index += 2
            continue
        if operator and word in _STDOUT_REDIRECTS:
            target = words[index + 1] if index + 1 < len(words) else ""
            if target not in _NULL_TARGETS:
                raise ScriptRefused(
                    f"`{word} {target}` writes a file behind edit/write's back. "
                    "Run the command without it and use write for the file."
                )
            if descriptor != "2":
                discard = True
            index += 2
            continue
        if operator and word in _INPUT_REDIRECTS:
            raise ScriptRefused(
                f"`{word}` feeds the program input, and nothing here does: it would "
                "only hang until the timeout. Pass a script with python -c instead."
            )
        if operator:
            raise ScriptRefused(
                f"`{word}` (subshells, background jobs, command substitution) is not "
                "supported. Use one plain command, or several joined by && || ; |."
            )
        kept.append(word)
        index += 1
    if not kept:
        raise ScriptRefused("a redirect with no command in front of it")
    return Stage(words=kept, discard_output=discard)


def parse(raw: str) -> list[Command]:
    """The script as commands in the order they would run.

    Raises ScriptRefused for anything outside the word-only sequence, and
    ValueError (from shlex) for an unbalanced quote.
    """
    commands: list[Command] = []
    connector = ""
    pipeline: list[list[str]] = [[]]
    for word in [*tokens(raw), Operator(";")]:
        operator = isinstance(word, Operator)
        if operator and word in CONNECTORS:
            if any(pipeline):
                if not all(pipeline):
                    raise ScriptRefused("a pipe with nothing on one side of it")
                commands.append(Command(connector, [_stage(words) for words in pipeline]))
            elif word != ";" or connector not in ("", ";"):
                raise ScriptRefused(f"`{word}` with no command before it")
            connector, pipeline = word, [[]]
        elif operator and word == "|":
            pipeline.append([])
        else:
            pipeline[-1].append(word)
    if not commands:
        raise ScriptRefused("there is no command in that script")
    for command in commands:
        for stage in command.stages[1:]:
            name = program_name(stage.words[0])
            if name not in FILTERS:
                raise ScriptRefused(
                    f"`| {stage.words[0]}` is not available after a pipe. What may follow "
                    f"a pipe: {', '.join(sorted(FILTERS - {'egrep', 'fgrep'}))}."
                )
    return commands


def program_name(word: str) -> str:
    """``C:/x/python3.exe`` -> ``python3``: the name an allow-list knows."""
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _count(value: str, default: int) -> tuple[int, bool]:
    """A head/tail count and whether it was written ``+N`` (tail: from line N)."""
    plus = value.startswith("+")
    digits = value.lstrip("+-")
    return (int(digits) if digits.isdigit() else default), plus


def _head_tail(name: str, args: list[str], text: str) -> str:
    lines = text.splitlines()
    count, plus, chars, all_but = 10, False, False, False
    index = 0
    while index < len(args):
        arg = args[index]
        value = None
        if arg in ("-n", "-c", "--lines", "--bytes") and index + 1 < len(args):
            value, chars = args[index + 1], arg in ("-c", "--bytes")
            index += 1
        elif arg.startswith(("--lines=", "--bytes=")):
            value, chars = arg.split("=", 1)[1], arg.startswith("--bytes")
        elif re.fullmatch(r"-[nc][+-]?\d+", arg):
            value, chars = arg[2:], arg[1] == "c"
        elif re.fullmatch(r"[-+]\d+", arg):
            value = arg[1:] if arg.startswith("-") else arg
        elif arg in ("-q", "-v", "-f", "--quiet", "--follow"):
            pass
        else:
            raise ScriptRefused(
                f"`{name} {arg}`: after a pipe, {name} reads the output before it -- "
                "drop the file name"
            )
        if value is not None:
            all_but = name == "head" and value.startswith("-")
            count, plus = _count(value, 10)
        index += 1
    if chars:
        if name == "head":
            return text[:-count] if all_but else text[:count]
        return text[count - 1 :] if plus else text[-count:] if count else ""
    if name == "head":
        kept = lines[:-count] if all_but else lines[:count]
    else:
        kept = lines[count - 1 :] if plus else lines[-count:] if count else []
    return "\n".join(kept)


_GREP_LONG_FLAGS = {
    "--invert-match": "v",
    "--ignore-case": "i",
    "--count": "c",
    "--line-number": "n",
    "--only-matching": "o",
    "--extended-regexp": "E",
    "--fixed-strings": "F",
}


def _basic_to_python(pattern: str) -> str:
    """A grep basic regular expression in Python's syntax.

    In a BRE, ``| + ? ( ) { }`` are literal and their backslashed forms are
    the operators -- the reverse of Python. Swapping only ``\\|`` used to
    leave a bare ``|`` meaning alternation, so `grep '|'` matched every line.
    """
    special = "|+?(){}"
    converted: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            following = pattern[index + 1]
            converted.append(following if following in special else character + following)
            index += 2
            continue
        converted.append(f"\\{character}" if character in special else character)
        index += 1
    return "".join(converted)


def _grep(args: list[str], text: str) -> tuple[str, bool]:
    flags: set[str] = set()
    patterns: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            patterns.extend(args[index + 1 : index + 2])
            break
        if arg.startswith("--"):
            if arg.startswith("--regexp="):
                patterns.append(arg.split("=", 1)[1])
            elif arg in _GREP_LONG_FLAGS:
                flags.add(_GREP_LONG_FLAGS[arg])
            index += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            for position, letter in enumerate(letters):
                if letter in "emABC":
                    value = letters[position + 1 :] or (
                        args[index + 1] if index + 1 < len(args) else ""
                    )
                    if not letters[position + 1 :]:
                        index += 1
                    if letter == "e":
                        patterns.append(value)
                    break
                flags.add(letter)
            index += 1
            continue
        if not patterns:
            patterns.append(arg)
        else:
            raise ScriptRefused(
                f"`grep ... {arg}`: after a pipe, grep reads the output before it -- "
                "drop the file name"
            )
        index += 1
    if not patterns:
        raise ScriptRefused("grep after a pipe needs a pattern")
    expressions = []
    for pattern in patterns:
        source = pattern
        if "F" in flags:
            source = re.escape(pattern)
        elif not flags & {"E", "P"}:
            source = _basic_to_python(pattern)
        if "w" in flags:
            source = rf"\b(?:{source})\b"
        if "x" in flags:
            source = rf"^(?:{source})$"
        try:
            expressions.append(re.compile(source, re.IGNORECASE if "i" in flags else 0))
        except re.error:
            expressions.append(
                re.compile(re.escape(pattern), re.IGNORECASE if "i" in flags else 0)
            )
    invert = "v" in flags
    hits = [
        (number, line)
        for number, line in enumerate(text.splitlines(), 1)
        if any(expression.search(line) for expression in expressions) != invert
    ]
    if "c" in flags:
        return str(len(hits)), bool(hits)
    if "q" in flags:
        return "", bool(hits)
    if "o" in flags and not invert:
        found = [
            match.group(0)
            for _, line in hits
            for expression in expressions
            for match in expression.finditer(line)
        ]
        return "\n".join(found), bool(hits)
    if "n" in flags:
        return "\n".join(f"{number}:{line}" for number, line in hits), bool(hits)
    return "\n".join(line for _, line in hits), bool(hits)


def _wc(args: list[str], text: str) -> str:
    wanted = {letter for arg in args if arg.startswith("-") for letter in arg[1:]}
    if any(not arg.startswith("-") for arg in args):
        raise ScriptRefused(_NO_FILE.format(name="wc"))
    counts = {
        "l": len(text.splitlines()),
        "w": len(text.split()),
        "c": len(text.encode()),
        "m": len(text),
    }
    order = [letter for letter in "lwmc" if letter in wanted] or ["l", "w", "c"]
    return " ".join(str(counts[letter]) for letter in order)


def _leading_int(value: str) -> int:
    digits = re.match(r"\d*", value)
    return int(digits.group()) if digits and digits.group() else 0


def _sort(args: list[str], text: str) -> str:
    flags: set[str] = set()
    key = 0
    separator: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-k", "-t") and index + 1 < len(args):
            if arg == "-k":
                key = _leading_int(args[index + 1])
            else:
                separator = args[index + 1]
            index += 2
            continue
        if arg.startswith("-k") and len(arg) > 2:
            key = _leading_int(arg[2:])
        elif arg.startswith("-t") and len(arg) > 2:
            separator = arg[2:]
        elif arg.startswith("-"):
            flags.update(arg[1:])
        else:
            raise ScriptRefused(_NO_FILE.format(name="sort"))
        index += 1
    lines = text.splitlines()

    def field_of(line: str) -> str:
        if not key:
            return line
        fields = line.split(separator) if separator else line.split()
        return (separator or " ").join(fields[key - 1 :])

    def order(line: str) -> tuple[float, str]:
        value = field_of(line)
        if "n" in flags or "h" in flags:
            number = re.match(r"\s*(-?\d+(?:\.\d+)?)", value)
            return (float(number.group(1)) if number else 0.0, value)
        return (0.0, value.lower() if "f" in flags else value)

    lines.sort(key=order, reverse="r" in flags)
    if "u" in flags:
        unique: list[str] = []
        for line in lines:
            if not unique or field_of(unique[-1]) != field_of(line):
                unique.append(line)
        lines = unique
    return "\n".join(lines)


def _uniq(args: list[str], text: str) -> str:
    flags = {letter for arg in args if arg.startswith("-") for letter in arg[1:]}
    groups: list[tuple[str, int]] = []
    for line in text.splitlines():
        if groups and groups[-1][0] == line:
            groups[-1] = (line, groups[-1][1] + 1)
        else:
            groups.append((line, 1))
    if "d" in flags:
        groups = [group for group in groups if group[1] > 1]
    if "u" in flags:
        groups = [group for group in groups if group[1] == 1]
    if "c" in flags:
        return "\n".join(f"{count:>7} {line}" for line, count in groups)
    return "\n".join(line for line, _ in groups)


def apply_filter(words: list[str], text: str) -> tuple[str, bool]:
    """One pipeline stage after the first, applied to ``text``.

    Returns the new text and whether the stage "succeeded" the way its exit
    code would say -- only grep can fail, by matching nothing, which is what
    ``... | grep x || echo none`` relies on.
    """
    name, args = program_name(words[0]), words[1:]
    if name in ("head", "tail"):
        return _head_tail(name, args, text), True
    if name in ("grep", "egrep", "fgrep"):
        extra = {"egrep": ["-E"], "fgrep": ["-F"]}.get(name, [])
        return _grep(extra + args, text)
    if name == "wc":
        return _wc(args, text), True
    if name == "sort":
        return _sort(args, text), True
    if name == "uniq":
        return _uniq(args, text), True
    if name == "cat":
        if any(not arg.startswith("-") for arg in args):
            raise ScriptRefused(_NO_FILE.format(name="cat"))
        return text, True
    raise ScriptRefused(f"`| {words[0]}` is not available after a pipe")


def clip_middle(text: str, max_lines: int, max_chars: int) -> str:
    """Keep the start and the end of a long output and say what went between.

    Codex truncates every exec result this way (``truncate_middle_chars``,
    ``utils/string``); this harness cut from the end, which is backwards for a
    command: pytest, ruff and pyright print their verdict and the failing
    test *last*, so a long run reached the model as its least useful part.
    One third head, two thirds tail -- the head says what ran, the tail what
    happened. Only for programs the shell tool ran: a read has an offset to
    page with, and news_fetch/fetch put the newest item first.
    """
    lines = text.splitlines()
    if len(lines) <= max_lines and len(text) <= max_chars:
        return text
    head_lines = max(1, max_lines // 3)
    tail_lines = max(1, max_lines - head_lines)
    head_chars = max(1, max_chars // 3)
    tail_chars = max(1, max_chars - head_chars)
    head: list[str] = []
    size = 0
    for line in lines[:head_lines]:
        if size + len(line) + 1 > head_chars:
            break
        head.append(line)
        size += len(line) + 1
    tail: list[str] = []
    size = 0
    for line in reversed(lines[len(head) :][-tail_lines:]):
        if size + len(line) + 1 > tail_chars:
            break
        tail.append(line)
        size += len(line) + 1
    tail.reverse()
    if not head and not tail:
        # One enormous line: keep its two ends.
        return (
            f"{text[:head_chars]}\n[... {len(text) - head_chars - tail_chars} characters "
            f"omitted from the middle ...]\n{text[-tail_chars:]}"
        )
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join(
        [
            *head,
            f"[... {omitted} lines omitted from the middle; this tool has no offset, so "
            "ask for less: narrow its arguments, or filter with | grep / | tail -n N ...]",
            *tail,
        ]
    )
