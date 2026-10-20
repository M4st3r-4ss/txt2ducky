# txt2ducky.py

Convert a plain-text or source file into a [Flipper Zero](https://flipperzero.one/) **Bad USB** payload (Ducky Script) that retypes the file inside a code editor on the target machine \u2014 with the original indentation, brackets, and tabs intact.

The whole point of the tool is layout fidelity. Naively typing source code through a HID keyboard emulator breaks almost immediately, because editors auto-indent on every `ENTER` and auto-close brackets. `txt2ducky.py` generates a payload that actively defeats those behaviours, and can **simulate and verify** that the payload reproduces the source before you ever copy it to the device.

> **Scope / intended use.** This is a convenience tool for a well-known hobbyist device. It reproduces a file you already have on a machine you control \u2014 for example, loading a code snippet onto an air-gapped lab machine that has no network or USB mass-storage. Only use it against systems you own or are explicitly authorised to test.

---

## Requirements

- Python **3.8+**
- No third-party packages
- Runs on Windows, Linux, and macOS (the *generator* is cross-platform; the generated payloads target a **Windows** editor)

---

## Quick start

```bash
# Convert a single file
python txt2ducky.py txt.txt DuckyOutput.txt

# Convert and prove the payload reproduces the source
python txt2ducky.py txt.txt DuckyOutput.txt --verify
```

Then copy the output file into `/ext/badusb/` on the Flipper SD card, open an empty editor buffer on the target, and run the payload from the Bad USB app.

---

## How it stays faithful (editor mode)

Typing code into a live editor is hostile territory. The payload is built to survive it:

1. **Indentation is never typed as text.** Every indentation column is produced with explicit `SPACE` (or `TAB`) key presses, never placed inside a `STRING` command. This keeps it separable from the editor's own auto-indent.
2. **Auto-indent is stripped after every newline.** Editors re-indent when you press `ENTER`; left alone that stacks into the classic `4 \u2192 8 \u2192 20 \u2192 36` column cascade. After each `ENTER` the payload sends `SHIFT-TAB` repeatedly to strip the editor's inserted indent. Extra presses are a no-op at column 0, so it can only ever remove leading whitespace \u2014 never your code.
3. **Chords use hyphens.** Stock Flipper firmware parses `SHIFT-TAB` / `SHIFT-END` as a single chord; `SHIFT END` (space-separated) would just press `SHIFT` and drop the second key. Use `--combo space` for forks that expect spaces.
4. **`ESC` before every `ENTER`.** This dismisses autocomplete popups (common in VS Code / PyCharm when a line ends in an identifier like `print`) so they can't swallow the newline.
5. **Tabs are expanded to spaces by default**, so mixed indentation can never raise a `TabError`. Use `--indent tab` or `--indent literal` to keep tabs.
6. **Bracket auto-closing is handled by overtyping** \u2014 editors that insert a matching `)` get the pair replaced when you type the same character; editors that don't simply receive it.

Because editor behaviour varies, editor mode is **best-effort**. For a guaranteed byte-exact result, use **PowerShell mode** (below).

---

## PowerShell mode (bulletproof)

Instead of typing the code into an editor, the payload opens PowerShell and **writes the file directly** from a base64 blob, then opens it. The source is never typed, so indentation, brackets, tabs, and non-ASCII characters are all exact. This is immune to every editor quirk.

```bash
python txt2ducky.py code.py out.txt --mode powershell --out-path C:\tmp\code.py
```

The base64 is typed in chunks (default 1200 chars per line) into a real PowerShell window rather than the Run dialog, which has a ~260-character limit.

---

## Verifying before you deploy

`--verify` runs the generated payload through a built-in editor **simulator** that models auto-indent on `ENTER`, then diffs the result against your source. It prints `PASS`, or a line-by-line diff of the first mismatches on `FAIL`. (Verification applies to editor mode; it's skipped in PowerShell mode, which doesn't type the code.)

```bash
python txt2ducky.py code.py out.txt --verify
```

Use `--dry-run` to print the payload to stdout instead of writing a file.

---

## Launching an editor first

By default the payload assumes a focused, empty editor buffer. You can have it launch one via the Run dialog:

```bash
# Launch VS Code and open a fresh empty file first
python txt2ducky.py code.py out.txt --open vscode --new-file
```

Recognised app names: `vscode`, `pycharm`, `sublime`, `notepad++`, `notepad`, `thonny`, `spyder`, `idle`, `cmd`, `powershell` \u2014 or pass any executable name directly. `--new-file` uses `CTRL-N` for most editors and a dedicated scratch-file sequence for PyCharm.

---

## Batch mode

Point the tool at a directory to convert every matching file into a parallel output directory, preserving names and subfolder structure.

```bash
# Every .txt under ./snippets -> ./payloads
python txt2ducky.py ./snippets ./payloads

# Recurse into subdirectories
python txt2ducky.py ./snippets ./payloads --recursive
```

The input and output directories must differ (so payloads can't overwrite sources). Use `--pattern` to match other extensions, `--suffix` to tag output names, and `--skip-existing` to leave already-generated payloads alone.

---

## Options

### Indentation
| Option | Default | Description |
|---|---|---|
| `--indent {spaces,tab,literal}` | `spaces` | How to emit leading whitespace. `spaces` flattens tabs safely; `literal` keeps the exact whitespace characters. |
| `--indent-width N` | `0` (detect) | Indent width used when expanding tabs. `0` detects it from the file (GCD of leading-space counts). |
| `--keep-trailing` | off | Keep trailing whitespace (stripped by default). |

### Reset strategy
| Option | Default | Description |
|---|---|---|
| `--reset {dedent,select,both,none}` | `dedent` | How to strip editor auto-indent after each `ENTER`. `dedent` = repeated `SHIFT-TAB` (safest); `select` = `HOME HOME SHIFT-END DELETE`; `both` = select then dedent. |
| `--combo {hyphen,space}` | `hyphen` | Chord syntax. `hyphen` for stock firmware; `space` for forks that expect it. |
| `--dedent-count N` | `0` (auto) | Number of `SHIFT-TAB` presses per line. Auto = max indent level + 2 (min 4). |

### Timing
| Option | Default | Description |
|---|---|---|
| `--line-delay MS` | `120` | Delay after `ENTER` before clearing auto-indent. |
| `--key-delay MS` | `30` | `DEFAULTDELAY` between keystrokes. |
| `--initial-delay MS` | `1500` | Delay before the payload starts. |
| `--no-esc` | off | Don't send `ESC` before `ENTER` (leave `ESC` on for IDEs). |

### Launching
| Option | Default | Description |
|---|---|---|
| `--open APP` | \u2014 | Launch an app first (see recognised names above, or any exe). |
| `--new-file` | off | Create a fresh empty buffer after launching. |
| `--app-delay MS` | `2500` | Time to wait for the app window. |
| `--layout LANG` | \u2014 | Emit `DUCKY_LANG` (only if your firmware supports it). |

### PowerShell mode
| Option | Default | Description |
|---|---|---|
| `--mode {editor,powershell}` | `editor` | `editor` types into the focused editor; `powershell` writes the file directly. |
| `--out-path PATH` | `%TEMP%\flipper_script.py` | Target path on the Windows PC. |
| `--then-open APP` | default handler | Open the written file with this app. |
| `--ps-chunk N` | `1200` | Base64 characters per typed line. |

### Output & batch
| Option | Default | Description |
|---|---|---|
| `--verify` | off | Simulate the payload and compare against the source. |
| `--dry-run` | off | Print the payload instead of writing it (single-file only). |
| `--pattern GLOB` | `*.txt` | Batch: files to convert. |
| `--recursive`, `-r` | off | Batch: include subdirectories. |
| `--suffix TEXT` | `""` | Batch: text inserted before the extension; empty keeps source names. |
| `--skip-existing` | off | Batch: leave payloads that already exist alone. |

Run `python txt2ducky.py --help` for the authoritative list.

---

## Examples

```bash
# Verify, then generate
python txt2ducky.py txt.txt DuckyOutput.txt --verify

# Keep real tabs, slow down between lines for a sluggish editor
python txt2ducky.py code.py out.txt --indent tab --line-delay 200

# Most aggressive reset plus explicit hyphen chords
python txt2ducky.py code.py out.txt --reset both --combo hyphen

# Byte-exact via PowerShell, write to a chosen path and open it
python txt2ducky.py code.py out.txt --mode powershell --out-path C:\tmp\code.py
```

---

## Notes & caveats

- **Non-ASCII characters** depend on the keyboard layout flashed on the Flipper. The tool warns when it sees them. PowerShell mode is the safe choice for non-ASCII content.
- **Mixed tabs and spaces** are flattened by the default `--indent spaces`; the tool warns rather than failing.
- Generated payloads target **Windows** editors (they drive the `Win+R` Run dialog). The generator itself runs anywhere.
- Editor mode is best-effort across editors; when correctness matters, prefer PowerShell mode and/or `--verify`.
