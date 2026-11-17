# LaTeX bridge (fork feature)

Links this QualCoder fork to the xarles LaTeX editor. The editor side (appendix with line numbers,
`\icite`/`\icitea`/`\iref`/`\iquote`, question overview, status bar) is described in xarles
`docs/spikes/qualcoder-integration.md` (issue we4OnTop/xarles#399). This page is the fork side; it
also serves as the spike write-up, because issues are disabled on this fork.

Everything runs on this computer. The fork only talks to `localhost`, and the AI edit only accepts a
local model.

## What the fork adds

| Feature | Where |
|---|---|
| Dragging coded text carries the coding: `application/x-qualcoder-segment` (JSON `ctid`, `fid`, `file`, `pos0`, `pos1`, `cid`) plus the same JSON as `data-qcref` in the HTML flavour. Dropped into the xarles editor it becomes `\iref{<ctid>}` | Code text window, select inside a coding and drag |
| *Copy LaTeX reference* puts `\iref{<ctid>}` on the clipboard | Context menu on a coded passage |
| *AI edit with diff review* | Menu *LaTeX*, or context menu on a selection |
| Status bar: *LaTeX-Editor: verbunden · synchronisiert 12:03* / *nicht erreichbar* | Main window, checked every 10 s |
| Heartbeat `<project>.qda/latex_bridge/heartbeat.json`, so xarles shows whether QualCoder is open | Written every 10 s while a project is open |

The editor address defaults to `http://127.0.0.1:8088/qualcoder/health` and can be changed with the
environment variable `QC_LATEX_EDITOR_URL`; anything that is not localhost is refused, and the request
bypasses any configured proxy.

## AI edit with diff review

1. Tick one or several files (or start from a selection in the coding window) and pick a mode:
   - **Glätten nach Regeln** — your own smoothing rules, one per line in the *Smoothing rules* box.
     They are numbered R1, R2, … and saved with the project in `latex_bridge/glaettungsregeln.txt`
     (a new project starts with fillers, repetitions, broken-off starts, light dialect). The prompt
     (`latex_bridge/prompts.py`) makes the model first check every sentence against every rule
     (`<pruefung>`), then re-check each planned change (rule? meaning unchanged? original verbatim?),
     and only then list the changes as JSON: rule number, verbatim original, replacement, reason.
     Fixed limits apply on top of any rule: meaning unchanged, speaker labels and line breaks kept,
     change only what a rule asks for.
   - **Zusammenfassen**, **Eigene Anweisung** (e.g. "diese Informationen einbauen").
   - **Überall markieren (Code setzen)** — "mark everywhere X with code Y": pick code Y, describe X.
2. *Request proposal*. Long transcripts are sent in pieces of about 4000 characters, cut at line breaks.
3. The proposal is a diff like a pull request: removed text red and struck through, new text green,
   codings yellow. Every smoothing change carries the badge of its rule (R1, R2, …); the list on the
   left shows file, line, rule and the change, the summary counts the changes per rule.
   - **Single click** on a proposal shows its file, line, full rule text and the model's reason below
     the diff. **Double-click** scrolls the diff to it and highlights it (switching to its file).
   - A change the model could not tie to one of your rules is marked **⚠ keine Regel** and starts
     unticked. Changes whose original is not in the text, that overlap another change or that would
     touch a line break are dropped; the summary says how many.
4. Untick what you reject, *Select all* / *Select none*, or *Request proposal* again.
5. *Apply selected* asks once more, then:
   - backs up the project database once (`latex_bridge/backups/data_<time>.qda`, SQLite backup API);
     if another QualCoder window still holds unsaved changes, it stops and says so instead;
   - keeps each original transcript once (`latex_bridge/originale/<id>_<name>.txt`, never overwritten);
   - applies text changes **through QualCoder's own edit dialog** (`DialogEditTextFile`), one delete
     and one insert at a time, so codings, annotations and case links move with the text exactly as
     with a manual edit; codings are inserted through QualCoder's database connection;
   - appends one entry per file to `latex_bridge/ki_protokoll.md`: time, mode, instruction, full
     prompt including the numbered rules, model and address, accepted vs. proposed changes, and the
     diff with rule and reason per change. This file documents the smoothing rules for the thesis.

Nothing is written before *Apply*. If a transcript changed in the meantime, applying is refused.

### Local model (llama.cpp / Gemma, or Ollama)

The dialog uses QualCoder's existing OpenAI-compatible connection and the AI profile selected in
*AI → Configuration*. It refuses to run unless that profile's API address is localhost.

llama.cpp's `llama-server` defaults to port 8080, which xarles' api already uses on the host. Start it
on another port, e.g.:

```bash
llama-server -m gemma-4-<variant>.gguf --host 127.0.0.1 --port 8081
```

and add a profile to `~/.qualcoder/config.ini` (or edit one in *AI → Configuration*):

```ini
[ai_model_llama.cpp local]
desc = Gemma 4 via llama.cpp on this computer
access_info_url =
large_model = gemma-4
large_model_context_window = 32000
fast_model = gemma-4
fast_model_context_window = 32000
reasoning_effort = default
api_base = http://localhost:8081/v1/
api_key = local
```

The API key must not be empty; any text works. The built-in *Ollama local AI* profile
(`http://localhost:11434/v1/`) works the same way.

### How far does QualCoder's own AI go?

The built-in AI agent can already create codings (`codes_create_text_coding` at permission level
*Sandboxed*, with undo in the chat), but it writes immediately. The dialog above adds the
review-first path the handoff asks for. QualCoder's other AI features keep using whatever profile is
configured — choose a local profile if no data may leave the computer.

## Hooks in upstream files

All code is in `src/qualcoder/latex_bridge/`. Upstream files only get one-line calls (and one upstream bug fix):

| File | Line |
|---|---|
| `src/qualcoder/__main__.py` | `latex_bridge.install_main(self)` in `MainWindow.__init__` (+ import) |
| `src/qualcoder/code_text.py` | `latex_bridge.install_code_text(self)` in `DialogCodeText.__init__` (+ import) |
| `src/qualcoder/code_text.py` | `latex_bridge.add_text_menu_actions(self, menu, position)` in `text_edit_menu` |
| `src/qualcoder/edit_textfile.py` | bug fix, not a hook: saving deleted a coding, annotation or case link that ends exactly at the end of the text (`newpos1 >= len(text)`; `pos1` is exclusive, so `>` is right). Every manual edit had this; the AI edit would have hit it on each transcript whose last line is coded. Worth sending upstream |

The drag data works by replacing `createMimeDataFromSelection` on the text edit instance; PyQt
dispatches the C++ virtual to it (covered by a test through `copy()`).

## Keeping up with upstream

```bash
git remote add upstream https://github.com/ccbogel/QualCoder.git   # once
git fetch upstream
git merge upstream/master
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m unittest tests.test_latex_bridge
```

A merge conflict can only touch the lines above. If upstream renames `DialogEditTextFile`,
`text_edit_menu`, `code_text`/`file_` on the coding dialog or `invoke_with_logging`, the tests fail
first.

## Dependencies and licence

No new dependency: standard library, PyQt6 and QualCoder's own modules. QualCoder is LGPL-3.0; this
fork and the new module stay under LGPL-3.0, with the upstream copyright notices kept.

## Tests

`tests/test_latex_bridge.py`, 21 tests.

- Without Qt: selection → coding, drag payload and HTML escaping, loopback-only status check,
  heartbeat, diff and classification, chunking, quote parsing, own rules (load, save, numbering,
  prompt), parsing the model's edits, locating them with rule, reason and guards, backup, original
  copy, log.
- With Qt offscreen: QualCoder's edit dialog moves codings correctly, a coding that ends with the text
  survives an edit, smoothing by rule → apply → backup → log, several files with double-click focus
  and detail, refusal while another window has unsaved changes, coding mode with a rejected proposal,
  a remote model is refused, drag data through the patched virtual, the reference menu, the
  main-window hook.

## Known limits

- Positions: Qt counts UTF-16 units, Python code points; transcripts with emoji or other non-BMP
  characters can shift positions after such a character (also on the xarles side).
- The change kinds are a heuristic for the overview; the diff itself is exact.
