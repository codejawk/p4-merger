#!/usr/bin/env python3
"""
p4.py — the Perforce layer for p4-merger.

Everything is guarded: if `p4` is missing or a command fails, callers get a
clean (ok, message) and can fall back or report. The ONLY writes we ever do are
revert / sync / edit / change / shelve — we NEVER run `p4 submit`.

Flow this module supports, in order:
  ensure_mapped -> revert -> sync -> (create_change once) -> edit -c CL
  ... caller writes the merged file ...
  -> shelve -c CL   (this is the "SCL" — a shelved changelist you can share)
"""

from __future__ import annotations

import os
import shutil
import subprocess


def have_p4():
    return shutil.which("p4") is not None


def _run(args, stdin=None):
    try:
        r = subprocess.run(["p4", *args], capture_output=True, text=True,
                           input=stdin, timeout=120)
        return r.returncode, r.stdout, r.stderr
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _first(*blobs):
    for b in blobs:
        for line in (b or "").splitlines():
            if line.strip():
                return line.strip()
    return ""


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #


def where(target):
    """Map a depot/client path to a local file. None => not in the client View."""
    rc, out, _ = _run(["where", target])
    if rc == 0 and out.strip():
        return out.strip().splitlines()[-1].split()[-1]
    return None


def _client_name():
    rc, out, _ = _run(["-ztag", "info"])
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("... clientName "):
                return line.split(" ", 2)[2].strip()
    return os.environ.get("P4CLIENT")


def ensure_mapped(target, auto_map):
    """Make sure `target` resolves to a local file. If it doesn't and auto_map
    is on, append a View line to the client spec, then re-check.
    Returns (local_path_or_None, [messages])."""
    msgs = []
    local = where(target)
    if local:
        return local, [f"✓ mapped   {target} → {local}"]
    if not target.startswith("//"):
        return None, [f"✗ {target} is not mapped and is not a depot path"]
    if not auto_map:
        return None, [f"✗ {target} is NOT in your client View (auto-map off)"]

    client = _client_name()
    if not client:
        return None, [f"✗ {target} not mapped; could not read client name to auto-map"]
    rc, spec, err = _run(["client", "-o"])
    if rc != 0:
        return None, [f"✗ could not read client spec: {_first(err)}"]

    # depot dir -> a matching client path, appended as a new View line
    depot_dir = target.rsplit("/", 1)[0]
    rel = depot_dir[2:].split("/", 1)[-1]  # drop //depot/
    view_line = f"\t{depot_dir}/... //{client}/{rel}/..."
    new_spec = _append_view(spec, view_line)
    rc, out, err = _run(["client", "-i"], stdin=new_spec)
    if rc != 0:
        return None, [f"✗ auto-map failed on `p4 client -i`: {_first(err, out)}"]
    msgs.append(f"⚑ auto-mapped {depot_dir}/... into client {client}")
    local = where(target)
    if local:
        msgs.append(f"✓ mapped   {target} → {local}")
        return local, msgs
    return None, msgs + [f"✗ still not mapped after auto-map: {target}"]


def _append_view(spec, view_line):
    """Append a mapping to the end of the View: block of a client spec.
    p4 resolves the View with last-match-wins, so appending our line means it
    takes effect even if a broader existing mapping precedes it."""
    lines = spec.split("\n")
    out, in_view = [], False
    for ln in lines:
        # a non-indented, non-empty line ends the View block
        if in_view and ln.strip() and not ln.startswith((" ", "\t")):
            out.append(view_line)
            in_view = False
        if ln.startswith("View:"):
            in_view = True
        out.append(ln)
    if in_view:
        out.append(view_line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# clean + latest + changelist
# --------------------------------------------------------------------------- #


def revert(target):
    rc, out, err = _run(["revert", target])
    return rc == 0, _first(out, err) or "reverted"


def sync(target):
    rc, out, err = _run(["sync", target])
    return rc == 0, _first(out, err) or "up to date"


def create_change(description):
    """Create an empty pending changelist. Returns (cl_number_or_None, msg)."""
    desc = "\n\t".join(description.strip().split("\n"))
    spec = f"Change:\tnew\n\nDescription:\n\t{desc}\n"
    rc, out, err = _run(["change", "-i"], stdin=spec)
    if rc != 0:
        return None, f"could not create CL: {_first(err, out)}"
    # "Change 12345 created."
    for tok in out.split():
        if tok.isdigit():
            return tok, f"created pending changelist {tok}"
    return None, f"CL created but number not parsed: {_first(out)}"


def edit(target, cl):
    rc, out, err = _run(["edit", "-c", str(cl), target])
    return rc == 0, _first(out, err) or "opened for edit"


def diff(target):
    rc, out, _ = _run(["diff", "-du", target])
    return out if rc == 0 else ""


def shelve(cl):
    """Shelve the changelist — this is the SCL. Never submits."""
    rc, out, err = _run(["shelve", "-c", str(cl)])
    return rc == 0, _first(out, err) or "shelved"


def describe_shelf(cl):
    rc, out, _ = _run(["describe", "-S", "-s", str(cl)])
    return out if rc == 0 else ""
