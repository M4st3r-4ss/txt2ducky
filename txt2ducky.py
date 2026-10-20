#!/usr/bin/env python3
"""
txt_to_ducky.py
===============

Convert a plain-text or source file into a Flipper Zero Bad USB payload
(ducky script) that reproduces the file inside a code editor.

The payload is written to be editor agnostic: VS Code, PyCharm, Sublime
Text, Notepad++, IDLE, Thonny, Spyder, Notepad, and anything else that
behaves like a normal Windows editor.

Why the code layout survives
----------------------------
1. Leading whitespace is NEVER placed inside a STRING command. Every
   indentation column is produced with explicit SPACE (or TAB) key
   presses.

2. Every editor auto-indents when ENTER is pressed. Left alone, that
   indent stacks with the indent the payload types, producing the classic
   4 -> 8 -> 20 -> 36 column cascade. After every ENTER the payload sends

       SHIFT-TAB  (repeated, default: max indent level + 2)

   which strips the editor's automatic indent. Extra presses are a no-op
   at column 0 in every mainstream editor, so this is self-correcting and
   can never remove text, only leading whitespace.

3. Key chords are written with hyphens (`SHIFT-TAB`, `SHIFT-END`).
   Flipper's parser chains MODIFIERS with spaces but does not treat
   `SHIFT END` as a chord - it presses SHIFT and ignores END. If you
   target a firmware that wants spaces, pass --combo space.

4. ESC is sent before every ENTER so an autocomplete popup cannot swallow
   the newline (common in VS Code / PyCharm when a line ends in an
   identifier such as `print`).

5. Tabs are expanded to spaces by default, so mixed indentation can never
   raise TabError. Use --indent tab or --indent literal to keep tabs.

6. Bracket auto-closing is handled by overtyping: editors that insert `)`
   automatically (VS Code, PyCharm, Sublime, Thonny, Spyder) replace the
   pair when the same character is typed. Editors that do not auto-close
   (Notepad++, IDLE) simply receive the character.

Usage
-----
    python txt_to_ducky.py txt.txt DuckyOutput.txt

    # Batch: every .txt under a folder -> a new folder, names unchanged
    python txt_to_ducky.py ./snippets ./payloads
    python txt_to_ducky.py ./snippets ./payloads --recursive

    # Launch the editor and create a fresh empty buffer first
    python txt_to_ducky.py code.py DuckyOutput.txt --open vscode --new-file

    # Explicit reset styles and chord syntax
    python txt_to_ducky.py code.py out.txt --reset both --combo hyphen

    # Self-check: simulate the payload against an auto-indenting editor
    python txt_to_ducky.py txt.txt DuckyOutput.txt --verify

    # Bulletproof alternative: write the file with PowerShell, then open it.
    # Immune to every editor quirk; the code is never typed.
    python txt_to_ducky.py code.py out.txt --mode powershell --out-path C:\\tmp\\code.py

Upload
------
Copy the generated file into /ext/badusb/ on the Flipper SD card, open an
empty editor buffer (or use --open/--new-file), then run it from Bad USB.

Requires Python 3.8+. No third-party packages. Windows, Linux, macOS.
"""

from __future__ import annotations

import argparse
import base64
import math
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# App names
# --------------------------------------------------------------------------

APP_MAP = {
    "vscode": "code",
    "vs code": "code",
    "vsc": "code",
    "code": "code",
    "pycharm": "pycharm",
    "sublime": "subl",
    "sublime text": "subl",
    "subl": "subl",
    "notepad++": "notepad++",
    "notepadplusplus": "notepad++",
    "notepad": "notepad",
    "idle": "idle",
    "thonny": "thonny",
    "spyder": "spyder",
    "cmd": "cmd",
    "powershell": "powershell",
}

# How each editor opens a fresh empty buffer after launch (best effort).
NEW_FILE_SEQUENCES = {
    # New Scratch File -> type the language -> create it.
    "pycharm": [
        "CTRL-ALT-SHIFT-INSERT",
        "DELAY 1200",
        "STRING Python",
        "DELAY 400",
        "ENTER",
        "DELAY 1200",
    ],
}
DEFAULT_NEW_FILE = ["CTRL-N"]


# --------------------------------------------------------------------------
# Indentation helpers
# --------------------------------------------------------------------------

def split_lead(line: str):
    """Return (leading_whitespace, remainder)."""
    stripped = line.lstrip(" \t")
    return line[: len(line) - len(stripped)], stripped


def detect_width(lines) -> int:
    """Guess the indent width from the leading-space counts (GCD)."""
    widths = set()
    for line in lines:
        if not line.strip():
            continue
        lead, _ = split_lead(line)
        if lead and "\t" not in lead:
            widths.add(len(lead))
    if not widths:
        return 4
    g = 0
    for w in widths:
        g = math.gcd(g, w)
    return g if g >= 2 else 4


def has_mixed_indent(lines) -> bool:
    for line in lines:
        lead, _ = split_lead(line)
        if "\t" in lead and " " in lead:
            return True
    return False


def indent_columns(lead: str, width: int) -> int:
    return len(lead.replace("\t", " " * max(width, 1)))


def max_indent_level(lines, width: int) -> int:
    level = 0
    for line in lines:
        lead, _ = split_lead(line)
        level = max(level, indent_columns(lead, width) // max(width, 1))
    return level


def indent_keys(lead: str, style: str, width: int):
    """Turn a line's leading whitespace into ducky key commands."""
    if style == "literal":
        return ["TAB" if ch == "\t" else "SPACE" for ch in lead]

    expanded = indent_columns(lead, width)

    if style == "spaces":
        return ["SPACE"] * expanded

    # style == "tab"
    units, remainder = divmod(expanded, max(width, 1))
    return ["TAB"] * units + ["SPACE"] * remainder


def chord(keys, style: str) -> str:
    """Emit a key chord. Hyphens are the only form Flipper parses as one."""
    return ("-" if style == "hyphen" else " ").join(keys)


def reset_keys(mode: str, combo_style: str, dedent_count: int):
    """Commands that strip whatever indentation the editor inserted."""
    cmds = []

    if mode in ("select", "both"):
        # HOME on a whitespace-only line goes to column 0 in every editor.
        # SHIFT-END must be a hyphen chord or it is just an END press.
        cmds += ["HOME", "HOME", chord(("SHIFT", "END"), combo_style), "DELETE"]

    if mode in ("dedent", "both"):
        # Outdent. Repeated presses are harmless at column 0, and this can
        # never delete text - it only removes leading whitespace.
        cmds += [chord(("SHIFT", "TAB"), combo_style)] * dedent_count

    return cmds


def emit_string(body: str):
    """Emit a line body, turning any embedded tab into a TAB key press."""
    out = []
    for i, part in enumerate(body.split("\t")):
        if i:
            out.append("TAB")
        if part:
            out.append("STRING " + part)
    return out


# --------------------------------------------------------------------------
# Payload builder (editor mode)
# --------------------------------------------------------------------------

def render_editor_body(lines, *, indent_style, width, reset_mode,
                       combo_style, dedent_count, line_delay, esc,
                       keep_trailing):
    """The part of the payload that actually types the file."""
    body = []
    reset = reset_keys(reset_mode, combo_style, dedent_count)

    for i, line in enumerate(lines):
        if i > 0:
            if esc:
                body.append("ESC")
            body.append("ENTER")
            if line_delay:
                body.append(f"DELAY {line_delay}")

        # Strip the editor's automatic indentation before typing our own.
        body.extend(reset)

        lead, rest = split_lead(line)
        if not keep_trailing:
            rest = rest.rstrip()

        if rest:
            body.extend(indent_keys(lead, indent_style, width))
            body.extend(emit_string(rest))

    return body


def render_editor(text: str, args) -> tuple:
    lines = text.splitlines()
    width = args.indent_width or detect_width(lines)
    dedent_count = args.dedent_count or max(
        4, max_indent_level(lines, width) + 2)

    body = render_editor_body(
        lines,
        indent_style=args.indent,
        width=width,
        reset_mode=args.reset,
        combo_style=args.combo,
        dedent_count=dedent_count,
        line_delay=args.line_delay,
        esc=args.esc,
        keep_trailing=args.keep_trailing,
    )

    out = [
        "REM -------------------------------------------------------",
        "REM  Generated by txt_to_ducky.py",
        "REM  Editor-agnostic typer: auto-indent safe, tab safe",
        "REM  Copy to /ext/badusb/ on the Flipper SD card",
        "REM -------------------------------------------------------",
        "",
    ]
    if args.layout:
        out.append(f"DUCKY_LANG {args.layout}")
    out.append(f"DEFAULTDELAY {args.key_delay}")
    out.append(f"DELAY {args.initial_delay}")
    out.append("")

    if args.open:
        exe = APP_MAP.get(args.open.strip().lower(), args.open.strip())
        out += [
            f"REM launch: {exe}",
            "GUI r",
            "DELAY 700",
            f"STRING {exe}",
            "ENTER",
            f"DELAY {args.app_delay}",
        ]
        if args.new_file:
            seq = NEW_FILE_SEQUENCES.get(args.open.strip().lower(),
                                         DEFAULT_NEW_FILE)
            out.append("REM fresh empty buffer")
            out.extend(seq)
            out.append("DELAY 1000")
        out.append("")

    out.append("REM ---- begin source ----")
    out.extend(body)
    out.append("REM ---- end source ----")
    return out, width, dedent_count


# --------------------------------------------------------------------------
# Payload builder (PowerShell mode)
# --------------------------------------------------------------------------

def ps_quote(value: str) -> str:
    return value.replace("'", "''")


def render_powershell(text: str, args) -> list:
    """Write the file with PowerShell, then open it.

    The code is never typed, so indentation, brackets, tabs and non-ASCII
    text are all exact. Commands are typed into an opened PowerShell
    window rather than the Run dialog, which has a ~260 character limit.
    """
    blob = base64.b64encode(text.encode("utf-8")).decode("ascii")
    chunk = max(200, args.ps_chunk)
    chunks = [blob[i:i + chunk] for i in range(0, len(blob), chunk)] or [""]

    if args.out_path:
        path_cmd = f"$p='{ps_quote(args.out_path)}'"
    else:
        path_cmd = "$p=Join-Path $env:TEMP 'flipper_script.py'"

    app = (args.then_open or "default").strip()
    if app.lower() in ("default", "auto", ""):
        launch = "Start-Process $p"
    else:
        exe = APP_MAP.get(app.lower(), app)
        launch = f"Start-Process -FilePath '{ps_quote(exe)}' -ArgumentList ('\"'+$p+'\"')"

    out = [
        "REM -------------------------------------------------------",
        "REM  Generated by txt_to_ducky.py (write-file mode)",
        "REM  PowerShell writes the source exactly, then opens it",
        "REM -------------------------------------------------------",
        "",
        f"DEFAULTDELAY {args.key_delay}",
        f"DELAY {args.initial_delay}",
        "GUI r",
        "DELAY 700",
        "STRING powershell -NoProfile -ExecutionPolicy Bypass",
        "ENTER",
        "DELAY 2000",
        "",
        "STRING $ErrorActionPreference='Stop'",
        "ENTER",
        "DELAY 200",
        "STRING $b=''",
        "ENTER",
        "DELAY 200",
    ]

    for c in chunks:
        out.append(f"STRING $b+='{c}'")
        out.append("ENTER")
        out.append("DELAY 200")

    out += [
        f"STRING {path_cmd}",
        "ENTER",
        "DELAY 200",
        "STRING $e=New-Object Text.UTF8Encoding $false",
        "ENTER",
        "DELAY 200",
        "STRING [IO.File]::WriteAllText($p,[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)),$e)",
        "ENTER",
        "DELAY 800",
        f"STRING {launch}",
        "ENTER",
        "DELAY 500",
        "REM done",
    ]
    return out


# --------------------------------------------------------------------------
# Self-check: simulate the payload against an auto-indenting editor
# --------------------------------------------------------------------------

def simulate(body, width: int):
    """Emulate the payload against an editor that auto-indents on ENTER."""
    buf = [""]
    pos = 0
    col = 0
    sel = None  # (start, end) or None

    for raw in body:
        cmd = raw.strip()
        if not cmd:
            continue
        up = cmd.upper()

        if (up.startswith("REM") or up.startswith("DELAY")
                or up.startswith("DEFAULTDELAY") or up.startswith("DUCKY_LANG")):
            continue
        if up == "ESC":
            sel = None
            continue
        if up.startswith("STRING "):
            s = cmd[len("STRING "):]
            line = buf[pos]
            buf[pos] = line[:col] + s + line[col:]
            col += len(s)
            continue

        key = up.replace(" ", "-")
        line = buf[pos]

        if key == "SHIFT-END":
            sel = (col, len(line))
            col = len(line)
            continue
        if key == "SHIFT-TAB":
            lead, rest = split_lead(line)
            if lead:
                removed = 1 if lead.startswith("\t") else min(width, len(lead))
                buf[pos] = line[removed:]
                col = max(0, col - removed)
            sel = None
            continue
        if key == "ENTER":
            left, right = line[:col], line[col:]
            indent = split_lead(left)[0]
            if left.rstrip().endswith(":"):
                indent += " " * width
            buf[pos] = left
            buf.insert(pos + 1, indent + right)
            pos += 1
            col = len(indent)
            sel = None
            continue
        if key == "HOME":
            lead, rest = split_lead(line)
            if not rest:
                col = 0
            else:
                col = 0 if col == len(lead) else len(lead)
            sel = None
            continue
        if key == "END":
            col = len(line)
            sel = None
            continue
        if key == "DELETE":
            if sel:
                a, b = sorted(sel)
                buf[pos] = line[:a] + line[b:]
                col = a
                sel = None
            elif col < len(line):
                buf[pos] = line[:col] + line[col + 1:]
            continue
        if key == "BACKSPACE":
            if sel:
                a, b = sorted(sel)
                buf[pos] = line[:a] + line[b:]
                col = a
                sel = None
            elif col > 0:
                buf[pos] = line[:col - 1] + line[col:]
                col -= 1
            continue
        if key == "SPACE":
            buf[pos] = line[:col] + " " + line[col:]
            col += 1
            continue
        if key == "TAB":
            buf[pos] = line[:col] + "\t" + line[col:]
            col += 1
            continue
        # Unknown commands are ignored by the simulator.

    return buf


def normalize(lines):
    out = [line.rstrip() for line in lines]
    while out and not out[-1].strip():
        out.pop()
    return out


def verify(source_text: str, body, width: int, quiet: bool = False) -> bool:
    expected = normalize(source_text.splitlines())
    produced = normalize(simulate(body, width))

    if expected == produced:
        if not quiet:
            print("verify: PASS - payload reproduces the source exactly "
                  "(simulated with editor auto-indent)")
        return True

    print("verify: FAIL - payload output differs from source")
    for i in range(max(len(expected), len(produced))):
        e = expected[i] if i < len(expected) else "<missing>"
        p = produced[i] if i < len(produced) else "<missing>"
        if e != p:
            print(f"  line {i + 1}:")
            print(f"    expected: {e!r}")
            print(f"    produced: {p!r}")
    return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert a text/source file into a Flipper Zero Bad USB "
                    "payload that reproduces it in any code editor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python txt_to_ducky.py txt.txt DuckyOutput.txt --verify
  python txt_to_ducky.py code.py out.txt --open vscode --new-file
  python txt_to_ducky.py code.py out.txt --indent tab --line-delay 120
  python txt_to_ducky.py code.py out.txt --mode powershell --out-path C:\\tmp\\code.py
  python txt_to_ducky.py code.py out.txt --reset both --combo hyphen
""",
    )

    p.add_argument("input",
                   help="source file (.txt, .py, anything) or a directory "
                        "to batch-convert")
    p.add_argument("output",
                   help="ducky script file to write, or the destination "
                        "directory when input is a directory")

    # indentation
    p.add_argument("--indent", choices=["spaces", "tab", "literal"],
                   default="spaces",
                   help="how to emit leading whitespace (default: spaces)")
    p.add_argument("--indent-width", type=int, default=0,
                   help="indent width used when expanding tabs (0 = detect)")
    p.add_argument("--keep-trailing", action="store_true",
                   help="keep trailing whitespace (stripped by default)")

    # reset strategy
    p.add_argument("--reset", choices=["dedent", "select", "both", "none"],
                   default="dedent",
                   help="how to strip editor auto-indent after each ENTER: "
                        "dedent = SHIFT-TAB repeats (default, safest); "
                        "select = HOME HOME SHIFT-END DELETE; "
                        "both = select then dedent")
    p.add_argument("--combo", choices=["hyphen", "space"], default="hyphen",
                   help="CHORD syntax: hyphen for stock Flipper firmware "
                        "(default), space for forks that expect it")
    p.add_argument("--dedent-count", type=int, default=0,
                   help="number of SHIFT-TAB presses (0 = auto)")

    # timing
    p.add_argument("--line-delay", type=int, default=120,
                   help="ms after ENTER before clearing auto-indent "
                        "(default: 120)")
    p.add_argument("--key-delay", type=int, default=30,
                   help="DEFAULTDELAY between keystrokes in ms (default: 30)")
    p.add_argument("--initial-delay", type=int, default=1500,
                   help="ms before the payload starts (default: 1500)")
    p.add_argument("--no-esc", dest="esc", action="store_false",
                   help="do not send ESC before ENTER (ESC dismisses "
                        "autocomplete popups; leave it on for IDEs)")

    # launching
    p.add_argument("--open", metavar="APP",
                   help="launch an app first: vscode, pycharm, sublime, "
                        "notepad++, notepad, thonny, spyder, idle, or an exe")
    p.add_argument("--new-file", action="store_true",
                   help="create a fresh empty buffer after launching the app")
    p.add_argument("--app-delay", type=int, default=2500,
                   help="ms to wait for the app window (default: 2500)")
    p.add_argument("--layout", metavar="LANG",
                   help="emit DUCKY_LANG (only if your firmware supports it)")

    # powershell mode
    p.add_argument("--mode", choices=["editor", "powershell"],
                   default="editor",
                   help="editor: type into the focused editor (default); "
                        "powershell: write the file directly, never type it")
    p.add_argument("--out-path", metavar="PATH",
                   help="powershell mode: target path on the Windows PC "
                        "(default: %%TEMP%%\\flipper_script.py)")
    p.add_argument("--then-open", metavar="APP",
                   help="powershell mode: open the written file with this app "
                        "(default: whatever opens .py files)")
    p.add_argument("--ps-chunk", type=int, default=1200,
                   help="powershell mode: base64 characters per typed line "
                        "(default: 1200)")

    p.add_argument("--verify", action="store_true",
                   help="simulate the payload against an auto-indenting "
                        "editor and compare with the source")
    p.add_argument("--dry-run", action="store_true",
                   help="print the payload instead of writing it")

    # batch mode
    p.add_argument("--pattern", default="*.txt",
                   help="batch mode: glob of files to convert "
                        "(default: *.txt)")
    p.add_argument("--recursive", "-r", action="store_true",
                   help="batch mode: also convert files in subdirectories")
    p.add_argument("--suffix", default="",
                   help="batch mode: text inserted before the extension; "
                        "leave empty to keep source names unchanged")
    p.add_argument("--skip-existing", action="store_true",
                   help="batch mode: leave payloads that already exist alone")
    return p


# --------------------------------------------------------------------------
# Single-file conversion
# --------------------------------------------------------------------------

def read_source(path: Path, quiet: bool = False) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        if not quiet:
            print(f"warning: {path.name} was not valid UTF-8; "
                  "bad bytes replaced", file=sys.stderr)
        return path.read_text(encoding="utf-8", errors="replace")


def convert_one(src: Path, dst: Path, args, quiet: bool = False) -> int:
    """Convert src into dst. Returns a summary line count, or -1 on failure."""
    text = read_source(src, quiet=quiet)
    lines = text.splitlines()
    width = args.indent_width or detect_width(lines)

    if not quiet:
        non_ascii = sorted({ch for ch in text if ord(ch) > 127})
        if non_ascii:
            print(f"warning: {src.name}: non-ASCII characters present "
                  f"({''.join(non_ascii[:10])}); they depend on the keyboard "
                  "layout flashed on the Flipper", file=sys.stderr)
        if has_mixed_indent(lines):
            print(f"warning: {src.name}: mixed tabs and spaces; "
                  "--indent spaces (default) flattens them safely",
                  file=sys.stderr)

    if args.mode == "powershell":
        payload = render_powershell(text, args)
        verify_body = None
        dedent_count = 0
    else:
        payload, width, dedent_count = render_editor(text, args)
        verify_body = render_editor_body(
            lines,
            indent_style=args.indent,
            width=width,
            reset_mode=args.reset,
            combo_style=args.combo,
            dedent_count=dedent_count,
            line_delay=args.line_delay,
            esc=args.esc,
            keep_trailing=args.keep_trailing,
        )

    if args.verify:
        if verify_body is None:
            print(f"verify: {src.name}: skipped (editor mode only)")
        elif not verify(text, verify_body, width, quiet=quiet):
            return -1

    rendered = "\n".join(payload) + "\n"

    if args.dry_run:
        sys.stdout.write(rendered)
        return len(payload)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered, encoding="utf-8", newline="\n")

    if not quiet:
        print(f"wrote {dst}")
        print(f"  source lines : {len(lines)}")
        if args.mode == "editor":
            print(f"  indent       : {args.indent} (width {width})")
            print(f"  reset        : {args.reset} (dedent x{dedent_count})")
        print(f"  payload lines: {len(payload)}")

    return len(payload)


# --------------------------------------------------------------------------
# Directory batch conversion
# --------------------------------------------------------------------------

def contains(parent: Path, child: Path) -> bool:
    """True when child is parent or sits underneath it."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def discover_sources(root: Path, pattern: str, recursive: bool,
                     exclude: Path | None = None) -> list:
    """All files under root matching pattern, sorted, output folder skipped."""
    it = root.rglob(pattern) if recursive else root.glob(pattern)
    found = []
    for path in it:
        if not path.is_file():
            continue
        if exclude is not None and contains(exclude, path):
            continue
        found.append(path)
    return sorted(found)


def payload_name(src: Path, suffix: str) -> str:
    """Destination file name, unchanged unless a suffix was requested."""
    name = Path(src.name)
    return f"{name.stem}{suffix}{name.suffix}" if suffix else name.name


def convert_batch(in_dir: Path, out_dir: Path, args) -> int:
    if out_dir.exists() and not out_dir.is_dir():
        print(f"error: output '{out_dir}' exists and is not a directory",
              file=sys.stderr)
        return 1

    same_dir = contains(in_dir, out_dir) and contains(out_dir, in_dir)
    if same_dir:
        print("error: input and output are the same directory; payloads "
              "would overwrite the sources", file=sys.stderr)
        return 1

    sources = discover_sources(in_dir, args.pattern, args.recursive,
                               exclude=out_dir)
    if not sources:
        print(f"error: no files matching '{args.pattern}' in {in_dir}",
              file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(sources)
    converted = skipped = failed = 0
    failures = []

    print(f"converting {total} file(s) from {in_dir} -> {out_dir}")

    for i, src in enumerate(sources, 1):
        rel = src.relative_to(in_dir)
        dst = out_dir / rel.parent / payload_name(src, args.suffix)
        label = f"[{i}/{total}] {src.name}"

        if same_dir and dst.resolve() == src.resolve():
            print(f"{label} -> skipped (same path)")
            skipped += 1
            continue
        if args.skip_existing and dst.exists():
            print(f"{label} -> skipped (exists)")
            skipped += 1
            continue

        try:
            if convert_one(src, dst, args, quiet=True) < 0:
                print(f"{label} -> FAILED verification")
                failed += 1
                failures.append(src.name)
                continue
        except OSError as exc:
            print(f"{label} -> FAILED: {exc}")
            failed += 1
            failures.append(src.name)
            continue

        print(f"{label} -> {dst.relative_to(out_dir)}")
        converted += 1

    print(f"done: {converted} converted, {skipped} skipped, {failed} failed")
    if failures:
        print("failed files:", ", ".join(failures), file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        print(f"error: input not found: {src}", file=sys.stderr)
        return 1

    if src.is_dir():
        if args.dry_run:
            print("error: --dry-run is not supported in batch mode; "
                  "convert a single file instead", file=sys.stderr)
            return 1
        return convert_batch(src, Path(args.output), args)

    if not src.is_file():
        print(f"error: not a regular file: {src}", file=sys.stderr)
        return 1

    dst = Path(args.output)
    if dst.exists() and dst.is_dir():
        dst = dst / payload_name(src, dst, args.suffix)

    return 0 if convert_one(src, dst, args) >= 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
