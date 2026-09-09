#!/usr/bin/env python3
"""
core.py — the merge brain for p4-merger.

Self-contained (no external deps, stdlib only). Three jobs:

  1. parse a unified-diff patch into per-file hunks
  2. LOCATE where each hunk belongs in the CURRENT file — by content, not by the
     patch's (unreliable) line numbers: anchor on the most distinctive single
     line, confirm with the surrounding context, break ties with the line number
  3. MERGE with the local `claude` CLI: hand it the current file + the hunk + the
     located region and let it make the edit, the way a person would

Nothing here talks to Perforce — that's p4.py. This module just turns
(file text, patch) into merged file text.
"""

from __future__ import annotations

import json
import re
import subprocess


# --------------------------------------------------------------------------- #
# patch parsing
# --------------------------------------------------------------------------- #

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _clean_path(raw):
    """Strip trailing tab-metadata and SVN/LSI '(working copy)' style suffixes."""
    if not raw:
        return ""
    s = raw.strip()
    if "\t" in s:
        s = s.split("\t", 1)[0].strip()
    s = re.sub(r"\s+\((?:working copy|revision\s+\d+|nonexistent)\)\s*$", "", s)
    return s.strip()


def _strip_ab(path):
    """git-style a/ b/ prefix -> bare path."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_patch(text):
    """Return a list of sections: {path, hunks:[{old_start, lines:[raw...]}]}."""
    lines = text.split("\n")
    sections, cur = [], None
    i, n = 0, len(lines)

    def flush():
        nonlocal cur
        if cur and (cur["hunks"] or cur["new_path"]):
            sections.append(cur)
        cur = None

    def fresh():
        return {"old_path": None, "new_path": None, "hunks": []}

    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git"):
            flush()
            cur = fresh()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", ln)
            if m:
                cur["old_path"], cur["new_path"] = m.group(1), m.group(2)
            i += 1
            continue
        if ln.startswith("Index: "):
            flush()
            cur = fresh()
            cur["new_path"] = ln[7:].strip()
            i += 1
            continue
        if ln.startswith("--- "):
            if cur is None or cur["hunks"]:
                flush()
                cur = fresh()
            cur["old_path"] = _clean_path(ln[4:])
            i += 1
            continue
        if ln.startswith("+++ "):
            if cur is None:
                cur = fresh()
            cur["new_path"] = _clean_path(ln[4:])
            i += 1
            continue
        m = _HUNK_RE.match(ln)
        if m and cur is not None:
            old_start = int(m.group(1))
            old_len = int(m.group(2) or 1)
            new_len = int(m.group(4) or 1)
            body, oc, nc = [], 0, 0
            i += 1
            # collect exactly the declared number of old/new lines, so a git
            # signature line ("-- ") after the hunk isn't miscounted
            while i < n and (oc < old_len or nc < new_len):
                b = lines[i]
                if b == "":
                    oc += 1
                    nc += 1
                elif b[0] == "+":
                    nc += 1
                elif b[0] == "-":
                    oc += 1
                elif b[0] == "\\":
                    pass
                else:  # ' ' context (and any stray line)
                    oc += 1
                    nc += 1
                body.append(b)
                i += 1
            cur["hunks"].append({"old_start": old_start, "lines": body})
            continue
        i += 1

    flush()
    for s in sections:
        raw = s["new_path"] or s["old_path"] or ""
        raw = _clean_path(raw)
        s["path"] = _strip_ab(raw) if raw != "/dev/null" else _strip_ab(
            _clean_path(s["old_path"] or ""))
    return sections


def patch_meta(text):
    """Pull CVE / CR ids out of the patch header for the CL description."""
    cve = re.search(r"CVE-\d{4}-\d{3,7}", text)
    cr = re.search(r"(?:CR[- ]?Id|CR Number|Change[- ]?Id)\s*[:=]?\s*([A-Za-z0-9-]+)",
                   text, re.I)
    subj = re.search(r"^Subject:\s*(?:\[PATCH[^\]]*\]\s*)?(.+)$", text, re.M)
    return {"cve": cve.group(0) if cve else None,
            "cr": cr.group(1) if cr else None,
            "subject": subj.group(1).strip() if subj else None}


def old_side(hunk):
    """Lines the hunk expects to already be in the file: context + removed."""
    out = []
    for b in hunk["lines"]:
        if b == "":
            out.append("")
        elif b[0] == " ":
            out.append(b[1:])
        elif b[0] == "-":
            out.append(b[1:])
    return out


# --------------------------------------------------------------------------- #
# locate — anchor line first, then confirm, line number only as a tiebreak
# --------------------------------------------------------------------------- #


def _distinctive(t):
    if len(t) < 4:
        return 0
    if t in ("{", "}", "};", "*/", "/*", "return;", "break;", "continue;"):
        return 0
    return len(t)


def locate(cur_lines, block, declared_line=None):
    """Locate `block` (a hunk's old side) in cur_lines. Returns a dict or None."""
    file_trim = [c.strip() for c in cur_lines]
    btrim = [b.strip() for b in block]
    m = len(btrim)
    if m == 0:
        return None
    signal = [(r, t) for r, t in enumerate(btrim) if t]
    if not signal:
        return None

    ranked = []
    for r, t in signal:
        d = _distinctive(t)
        if d:
            hits = [i for i, ft in enumerate(file_trim) if ft == t]
            if hits:
                ranked.append((len(hits), -d, r, t, hits))
    if not ranked:
        return None
    ranked.sort()  # rarest first, then most distinctive

    starts = {}
    for _n, _d, r, _t, hits in ranked[:3]:
        for f in hits:
            s = f - r
            if 0 <= s and s + m <= len(cur_lines):
                starts.setdefault(s, (_t, f))

    if not starts:
        return None

    def confirm(s):
        return sum(1 for r, t in signal if file_trim[s + r] == t)

    scored = []
    for s, (anchor_t, anchor_f) in starts.items():
        got = confirm(s)
        gap = abs((s + 1) - declared_line) if declared_line else 0
        scored.append({"start": s, "confirmed": got, "signal": len(signal),
                       "score": round(got / len(signal), 3), "gap": gap,
                       "anchor": anchor_t, "anchor_line": anchor_f + 1})

    best = max(scored, key=lambda c: (c["score"], -c["gap"]))
    if best["score"] < 0.5:
        return None
    ties = [c for c in scored if c["score"] == best["score"]]
    best["ambiguous"] = len(ties) > 1
    best["confident"] = best["score"] >= 0.75 and not best["ambiguous"]
    best["start_1"] = best["start"] + 1
    best["end"] = best["start"] + m
    best["snippet"] = "\n".join(cur_lines[best["start"]:best["end"]][:24])
    return best


def locate_all(cur_lines, section):
    notes = []
    for idx, h in enumerate(section["hunks"], 1):
        note = locate(cur_lines, old_side(h), declared_line=h["old_start"])
        if note is None:
            note = {"idx": idx, "found": False, "start_1": None}
        else:
            note["idx"] = idx
            note["found"] = True
        notes.append(note)
    return notes


# --------------------------------------------------------------------------- #
# claude CLI
# --------------------------------------------------------------------------- #


def claude_available():
    import shutil
    return shutil.which("claude") is not None


def ask_claude(prompt, model=None):
    cmd = ["claude", "-p", prompt]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def stream_claude(prompt, on_text, model=None):
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json",
           "--verbose", "--include-partial-messages"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError:
        return None
    final = ""
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "stream_event":
            ev = o.get("event", {})
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    t = d.get("text", "")
                    final += t
                    on_text(t)
        elif o.get("type") == "result":
            final = o.get("result", final) or final
    proc.wait()
    return final


def _strip_fences(text):
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z0-9]*\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #

ANALYSE_PROMPT = """\
In 2-3 plain sentences, say what this vendor patch changes and why (the security \
intent). No preamble, no markdown, just the sentences.

=== PATCH ===
{patch}
"""

MERGE_PROMPT = """\
Integrate this vendor security patch into a source file that has DRIFTED from the \
revision the patch was written against. The patch's @@ line numbers are \
unreliable — IGNORE them. Use each hunk's context lines and its removed ('-') \
lines to find the right spot in the CURRENT file, then make the change the way a \
careful engineer would when the old context no longer matches byte-for-byte.

Hard rules:
- Output the COMPLETE merged file and NOTHING else. No prose, no markdown fences.
- Change ONLY what the patch requires. Every other line stays byte-for-byte \
identical (indentation, comments, blank lines included).
- Match the current file's own indentation/brace style for lines you add.
- If the change is already present, leave it — do not duplicate it.

=== CURRENT FILE ({path}) ===
{current}

=== VENDOR PATCH (context/line numbers may be stale) ===
{patch}

=== WHERE THE CHANGES LIKELY GO (located by content, not line number) ===
{hints}
"""


def hints_text(notes):
    out = []
    for n in notes:
        if not n.get("found"):
            out.append(f"Hunk {n['idx']} → no confident location; place from context.")
            continue
        cert = ("high confidence" if n["confident"]
                else "LOW confidence (context ambiguous — verify)"
                if n.get("ambiguous") else "medium confidence")
        out.append(f"Hunk {n['idx']} → lines {n['start_1']}-{n['end']} "
                   f"({n['confirmed']}/{n['signal']} context lines confirmed, {cert}; "
                   f"anchored on `{n['anchor']}` at line {n['anchor_line']}).\n"
                   + n["snippet"])
    return "\n\n".join(out)


def merge_file_text(current, patch_text, path, notes, model=None, on_text=None):
    """Return merged file text (or None if Claude gave nothing usable)."""
    prompt = MERGE_PROMPT.format(path=path, current=current,
                                 patch=patch_text[:8000], hints=hints_text(notes))
    out = stream_claude(prompt, on_text, model) if on_text \
        else ask_claude(prompt, model)
    merged = _strip_fences(out)
    if not merged.strip() or len(merged) < len(current) * 0.5:
        return None
    if not merged.endswith("\n"):
        merged += "\n"
    return merged


def unified_diff(before, after, path):
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))
