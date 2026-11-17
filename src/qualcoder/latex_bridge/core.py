"""
This file is part of QualCoder (fork: LaTeX bridge, see docs/latex_bridge.md).

QualCoder is free software: you can redistribute it and/or modify it under the
terms of the GNU Lesser General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version.

Logic of the LaTeX bridge without Qt, so it is testable on its own: drag payloads, the heartbeat
the LaTeX editor reads, the editor's health check, and the AI proposals (diff, classification,
coding quotes, backup, original copy and the log). Standard library only.
"""

import datetime
import difflib
import html
import json
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

MIME = "application/x-qualcoder-segment"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
EDITOR_URL = os.environ.get("QC_LATEX_EDITOR_URL", "http://127.0.0.1:8088/qualcoder/health")
FOLDER = "latex_bridge"  # inside the QualCoder project folder
CHUNK_CHARS = 4000  # what one local-model request covers

FILLERS = {
    "äh", "ähm", "öh", "öhm", "hm", "hmm", "mhm", "mh", "halt", "eben", "sozusagen", "quasi",
    "irgendwie", "naja", "na", "ne", "gell", "also", "ja", "genau", "so", "eigentlich", "einfach",
}
CODE_SET = "Code gesetzt"
NO_RULE = "⚠ keine Regel"
RULES_FILE = "glaettungsregeln.txt"


# ---------------------------------------------------------------- LaTeX editor link


def is_loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and parsed.hostname in LOOPBACK


def write_heartbeat(project_path: str, version: str = "") -> None:
    """Tells the LaTeX editor's bridge (which reads the project folder read-only) that QualCoder is open."""
    folder = Path(project_path) / FOLDER
    folder.mkdir(exist_ok=True)
    tmp = folder / "heartbeat.json.tmp"
    tmp.write_text(json.dumps({"time": time.time(), "version": version}), encoding="utf-8")
    tmp.replace(folder / "heartbeat.json")


def editor_status(project_path: str, url: str = EDITOR_URL, timeout: float = 2.0) -> tuple[bool, str]:
    """(reachable, text) for the status bar. Only loopback URLs are ever contacted."""
    if not is_loopback_url(url):
        return False, "LaTeX-Editor: nur localhost erlaubt"
    # No proxy: a configured system proxy must never see this request.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            health = json.loads(response.read(100_000))
    except (OSError, ValueError):
        return False, "LaTeX-Editor: nicht erreichbar"
    synced = health.get("projects", {}).get(os.path.basename(os.path.normpath(project_path or "")))
    if not synced:
        return True, "LaTeX-Editor: verbunden (dieses Projekt noch nicht synchronisiert)"
    when = str(synced.get("lastSync", ""))[11:16]
    return True, f"LaTeX-Editor: verbunden · synchronisiert {when}"


def segment_for_selection(codings: list[dict], start: int, end: int) -> dict | None:
    """The coding a selection (or, when start == end, a click) refers to.

    Prefer the smallest coding that contains the selection, else the one overlapping it most."""
    containing = [c for c in codings if c["pos0"] <= start and end <= c["pos1"]]
    if containing:
        return min(containing, key=lambda c: c["pos1"] - c["pos0"])
    overlapping = [(min(end, c["pos1"]) - max(start, c["pos0"]), c) for c in codings]
    overlapping = [(size, c) for size, c in overlapping if size > 0]
    return max(overlapping, key=lambda o: o[0])[1] if overlapping else None


def drag_payload(coding: dict, file_: dict) -> str:
    return json.dumps({
        "ctid": coding["ctid"], "fid": file_["id"], "file": file_["name"],
        "pos0": coding["pos0"], "pos1": coding["pos1"], "cid": coding["cid"],
    }, ensure_ascii=False)


def drag_html(payload: str, text: str) -> str:
    """HTML flavour carrying the payload, for browsers that drop custom types from native apps."""
    return f'<span data-qcref="{html.escape(payload)}">{html.escape(text)}</span>'


def iref(ctid: int) -> str:
    return f"\\iref{{{ctid}}}"


# ---------------------------------------------------------------- AI proposals

_TOKEN = re.compile(r"\s+|\w+|[^\w\s]")


def tokens(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text)]


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def chunks(text: str, start: int, end: int, size: int = CHUNK_CHARS) -> list[tuple[int, int]]:
    """[start, end) cut at line breaks into pieces a local model handles in one request."""
    pieces = []
    while start < end:
        stop = end if end - start <= size else text.rfind("\n", start, start + size) + 1
        if stop <= start:
            stop = min(end, start + size)
        pieces.append((start, stop))
        start = stop
    return pieces


def classify(text: str, pos0: int, pos1: int, new: str) -> str:
    old = text[pos0:pos1]
    old_words, new_words = words(old), words(new)
    if old_words and not new_words:
        if all(w in FILLERS for w in old_words):
            return "Füllwort entfernt"
        phrase = " ".join(old_words)
        before = " ".join(words(text[max(0, pos0 - len(old) - 20):pos0]))
        after = " ".join(words(text[pos1:pos1 + len(old) + 20]))
        if before.endswith(phrase) or after.startswith(phrase):
            return "Wiederholung entfernt"
    if old.strip() and old.lower() == new.lower():
        return "Schreibweise angepasst"
    if old_words and sorted(old_words) == sorted(new_words):
        return "Satz umgestellt"
    if len(old) > 40 and len(new) < 0.6 * len(old):
        return "zusammengefasst"
    if not old_words:
        return "ergänzt"
    return "geändert"


def diff_changes(original: str, revised: str, offset: int = 0) -> list[dict]:
    """Word-level changes turning original into revised, as positions in the full transcript.

    Aligned case-insensitively, so "äh das" -> "Das" reads as a removed filler plus a capitalisation,
    and a removal is split per word, so a filler and a repetition removed together are told apart."""
    a, b = tokens(original), tokens(revised)
    matcher = difflib.SequenceMatcher(None, [t[2].lower() for t in a], [t[2].lower() for t in b], autojunk=False)
    changes = []

    def add(p0, p1, new):
        changes.append({"pos0": offset + p0, "pos1": offset + p1, "old": original[p0:p1], "new": new,
                        "kind": classify(original, p0, p1, new)})

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for (p0, p1, old), (_s, _e, new) in zip(a[i1:i2], b[j1:j2]):
                if old != new:
                    add(p0, p1, new)
        elif tag == "delete":
            cuts, seen = [a[i1][0]], False
            for k in range(i1, i2):
                if a[k][2].isspace():
                    continue
                if seen:  # a new piece starts at the whitespace before the next word
                    cuts.append(a[k - 1][0] if k > i1 and a[k - 1][2].isspace() else a[k][0])
                seen = True
            cuts.append(a[i2 - 1][1])
            for p0, p1 in zip(cuts, cuts[1:]):
                add(p0, p1, "")
        else:
            p0 = a[i1][0] if i1 < len(a) else len(original)
            add(p0, a[i2 - 1][1] if i2 > i1 else p0, "".join(t[2] for t in b[j1:j2]))
    return changes


def parse_quotes(answer: str) -> list[str]:
    """The quotes from a model answer that should be a JSON array of strings (or of {"quote": ...})."""
    start, end = answer.find("["), answer.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(answer[start:end + 1])
    except ValueError:
        return []
    quotes = [i.get("quote", "") if isinstance(i, dict) else i for i in items if isinstance(i, (str, dict))]
    return [q.strip() for q in quotes if isinstance(q, str) and q.strip()]


def coding_changes(text: str, quotes: list[str], start: int, end: int, existing: list[tuple[int, int]]) -> list[dict]:
    """Where each quote occurs in [start, end), whitespace-tolerant; skips spans already coded with the code."""
    spans = set()
    for quote in quotes:
        pattern = re.compile(r"\s+".join(re.escape(w) for w in quote.split()))
        for match in pattern.finditer(text, start, end):
            spans.add(match.span())
    return [
        {"pos0": p0, "pos1": p1, "old": text[p0:p1], "new": text[p0:p1], "kind": CODE_SET}
        for p0, p1 in sorted(spans) if (p0, p1) not in existing
    ]


def parse_rules(text: str) -> list[str]:
    """One rule per line; a leading "1." or "R1:" is dropped, since the numbering is ours."""
    return [re.sub(r"^\s*(?:R?\d+\s*[.:)]\s*)", "", line).strip() for line in text.splitlines() if line.strip()]


def load_rules(project_path: str, default: list[str]) -> list[str]:
    try:
        rules = parse_rules(Path(project_path, FOLDER, RULES_FILE).read_text(encoding="utf-8"))
    except OSError:
        rules = []
    return rules or list(default)


def save_rules(project_path: str, rules: list[str]) -> None:
    (project_folder(project_path) / RULES_FILE).write_text("\n".join(rules) + "\n", encoding="utf-8")


def parse_edits(answer: str) -> list[dict]:
    """The edits of a smoothing answer: the JSON array between <aenderungen> tags (or the array left
    after removing the <pruefung> part). Malformed entries are skipped."""
    tagged = re.search(r"<aenderungen>(.*?)(?:</aenderungen>|$)", answer, re.S)
    body = tagged.group(1) if tagged else re.sub(r"<pruefung>.*?</pruefung>", "", answer, flags=re.S)
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(body[start:end + 1])
    except ValueError:
        try:  # local models often produce almost-JSON; json_repair is already a QualCoder dependency
            import json_repair
            items = json_repair.loads(body[start:end + 1])
        except Exception:
            return []
    edits = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("original"), str) or not item["original"].strip():
            continue
        rule = re.search(r"\d+", str(item.get("regel", "")))
        edits.append({"rule": int(rule.group()) if rule else 0, "original": item["original"],
                      "new": str(item.get("ersetzung") or ""), "reason": str(item.get("begruendung") or "").strip()})
    return edits


def short_rule(rule: str) -> str:
    return rule if len(rule) <= 32 else rule[:30].rsplit(" ", 1)[0] + " …"


def rule_changes(text: str, edits: list[dict], start: int, end: int, rules: list[str]) -> tuple[list[dict], int]:
    """(changes, dropped) for the model's edits inside [start, end).

    Each original is searched from the previous edit on (models list them in text order), tolerant to
    whitespace. Dropped: not found, overlapping an earlier edit, no change, or touching a line break."""
    changes, taken, cursor, dropped = [], [], start, 0
    for edit in edits:
        pattern = re.compile(r"\s+".join(re.escape(w) for w in edit["original"].split()))
        match = pattern.search(text, cursor, end) or pattern.search(text, start, end)
        if not match:
            dropped += 1
            continue
        p0, p1 = match.span()
        new = edit["new"]
        if not new.strip():  # a removal takes one neighbouring space with it, so no double space remains
            new = ""
            if p1 < end and text[p1] == " " and (p0 == start or text[p0 - 1] in " \n"):
                p1 += 1
            elif p0 > start and text[p0 - 1] == " " and (p1 == end or text[p1] in ",.;:!?\n"):
                p0 -= 1
        if new == text[p0:p1] or text[p0:p1].count("\n") != new.count("\n") \
                or any(p0 < b and a < p1 for a, b in taken):
            dropped += 1
            continue
        taken.append((p0, p1))
        cursor = p1
        valid = 1 <= edit["rule"] <= len(rules)
        changes.append({
            "pos0": p0, "pos1": p1, "old": text[p0:p1], "new": new, "rule": edit["rule"] if valid else 0,
            "kind": f"R{edit['rule']} {short_rule(rules[edit['rule'] - 1])}" if valid else NO_RULE,
            "reason": edit["reason"],
        })
    return sorted(changes, key=lambda c: c["pos0"]), dropped


def summary(text: str | None, changes: list[dict]) -> str:
    """"12 Änderungen · Zeilen 3–40 · R1 …: 7, R2 …: 5"; over several files the file count instead of lines."""
    if not changes:
        return "Keine Änderungen vorgeschlagen."
    kinds = {}
    for c in changes:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    files = {c["file"] for c in changes if c.get("file")}
    lines = sorted(c.get("line") or line_of(text or "", c["pos0"]) for c in changes)
    where = f"{len(files)} Dateien" if len(files) > 1 else \
        f"Zeile {lines[0]}" if lines[0] == lines[-1] else f"Zeilen {lines[0]}–{lines[-1]}"
    return f"{len(changes)} Änderungen · {where} · " + ", ".join(f"{k}: {n}" for k, n in sorted(kinds.items()))


def diff_html(text: str, start: int, end: int, items: list[tuple[int, dict]], selected: set[int],
              focus: int | None = None) -> str:
    """Inline diff like a pull request: removed red and struck through, added green, codings yellow.

    items are (index, change) of one file; every change gets the anchor c<index> to scroll to and,
    for smoothing, the badge of its rule (R2, or a red ? without a valid rule). Unchecked changes are
    greyed out; the focused one is bold and stronger in colour."""
    out, pos = [], start
    for index, c in items:
        out.append(html.escape(text[pos:c["pos0"]]))
        on, hot = index in selected, index == focus
        bold = "font-weight:bold;" if hot else ""
        grey = "" if on or hot else "color:#9e9e9e;"
        out.append(f'<a name="c{index}"></a>')
        if c["kind"] == CODE_SET:
            background = "#ffb300" if hot else "#ffe9a8" if on else "#eeeeee"
            out.append(f'<span style="background:{background};{bold}">{html.escape(c["old"])}</span>')
        else:
            if c["old"]:
                background = "#ff8a80" if hot else "#ffd7d5"
                out.append(f'<span style="background:{background};text-decoration:line-through;{grey}{bold}">'
                           f'{html.escape(c["old"])}</span>')
            if c["new"]:
                background = "#69f0ae" if hot else "#ccffd8"
                out.append(f'<span style="background:{background};{grey}{bold}">{html.escape(c["new"])}</span>')
        if "rule" in c:
            badge = f'R{c["rule"]}' if c["rule"] else "?"
            out.append(f'<sup style="color:{"#1565c0" if c["rule"] else "#c62828"};{bold}">{badge}</sup>')
        pos = c["pos1"]
    out.append(html.escape(text[pos:end]))
    return '<div style="white-space:pre-wrap;font-family:sans-serif">' + "".join(out) + "</div>"


# ---------------------------------------------------------------- safety net and log


def project_folder(project_path: str, *parts: str) -> Path:
    folder = Path(project_path, FOLDER, *parts)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(conn: sqlite3.Connection, project_path: str) -> Path:
    """A consistent copy of the open project database, taken through SQLite's backup API."""
    if conn.in_transaction:
        # sqlite3's backup retries forever while the same connection holds an open write transaction.
        raise RuntimeError("Ein anderes QualCoder-Fenster hat noch nicht gespeicherte Änderungen. Bitte dort "
                           "speichern oder das Fenster schließen und dann erneut übernehmen.")
    target = project_folder(project_path, "backups") / f"data_{stamp()}.qda"
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    return target


def save_original(project_path: str, fid: int, name: str, text: str) -> Path:
    """The transcript as it was before its first AI edit; never overwritten."""
    safe = re.sub(r"[^\w.-]+", "_", name)
    target = project_folder(project_path, "originale") / f"{fid}_{safe}.txt"
    if not target.exists():
        target.write_text(text, encoding="utf-8")
    return target


def append_log(project_path: str, *, file_name: str, mode: str, instruction: str, prompt: str, model: str,
               text: str, changes: list[dict], proposed: int, backup_path: Path) -> Path:
    """Markdown log of every accepted AI change, usable as documentation of the smoothing rules."""
    log = project_folder(project_path) / "ki_protokoll.md"
    lines = [
        f"\n## {datetime.datetime.now():%Y-%m-%d %H:%M} · {mode} · {file_name}\n",
        f"- Modell: {model}",
        f"- Anweisung: {instruction or '–'}",
        f"- Übernommen: {len(changes)} von {proposed} Vorschlägen ({summary(text, changes)})",
        f"- Backup vorher: {backup_path.name}",
        "- Prompt:\n\n```text\n" + prompt.strip() + "\n```\n",
        "```diff",
    ]
    for c in changes:
        lines.append(f"@@ Zeile {c.get('line') or line_of(text, c['pos0'])} · {c['kind']}")
        if c.get("reason"):
            lines.append(f"# Begründung: {c['reason']}")
        if c["kind"] == CODE_SET:
            lines.append("+ [Code] " + c["old"].replace("\n", " "))
            continue
        if c["old"]:
            lines.append("- " + c["old"].replace("\n", "\n- "))
        if c["new"]:
            lines.append("+ " + c["new"].replace("\n", "\n+ "))
    lines.append("```\n")
    with log.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return log
