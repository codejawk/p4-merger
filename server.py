#!/usr/bin/env python3
"""
server.py — p4-merger web app.

A point-and-click front end over the whole flow:

  files (one or many) + a patch
    -> ensure each file is mapped into your client (auto-map if you allow it)
    -> revert + sync to latest
    -> create ONE pending changelist and open the files into it
    -> Claude CLI analyses the patch and merges it into each file
    -> shelve the changelist  == the SCL, whose number is returned
    (never `p4 submit` — the shelved CL is yours to review and share)

Stdlib only. Run:  python3 server.py   then open http://127.0.0.1:8770
Test without Perforce:  tick "no p4 (merge local files only)" in the UI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core  # noqa: E402
import p4 as P4  # noqa: E402


# --------------------------------------------------------------------------- #
# the job — a generator of progress events
# --------------------------------------------------------------------------- #


def run_job(files, patch_text, description=None, model=None,
            auto_map=True, use_p4=True, do_shelve=True):
    """Yield event dicts describing the run. The last event is type 'done'."""
    def ev(**k):
        return k

    use_p4 = use_p4 and P4.have_p4()
    sections = core.parse_patch(patch_text)
    meta = core.patch_meta(patch_text)
    if not sections:
        yield ev(type="log", level="err", msg="No file sections found in the patch.")
        yield ev(type="done", ok=False, cl=None, files=[])
        return

    if not description:
        bits = [b for b in (meta.get("cve"), meta.get("cr")) if b]
        description = "p4-merger: " + (meta.get("subject") or "merge vendor patch")
        if bits:
            description += "  [" + " ".join(bits) + "]"

    yield ev(type="log", level="info",
             msg=f"patch: {len(sections)} file section(s); "
                 f"cve={meta.get('cve') or '-'} cr={meta.get('cr') or '-'}")
    yield ev(type="log", level="info",
             msg=f"p4: {'on' if use_p4 else 'OFF (local merge only)'} · "
                 f"claude: {'found' if core.claude_available() else 'MISSING'}")

    # pair each requested file with a patch section (by basename)
    by_base = {}
    for s in sections:
        by_base.setdefault(os.path.basename(s["path"]), s)
    if len(files) == 1 and len(sections) == 1:
        pairs = [(files[0], sections[0])]
    else:
        pairs = []
        for f in files:
            sec = by_base.get(os.path.basename(f.rstrip("/")))
            pairs.append((f, sec))

    # ---- create the changelist up front (so files open directly into it) ----
    cl = None
    if use_p4:
        cl, msg = P4.create_change(description)
        yield ev(type="log", level="info" if cl else "err", msg="p4 change: " + msg)

    results = []
    for target, sec in pairs:
        r = {"target": target, "status": "pending", "p4": [], "before": "",
             "after": "", "diff": "", "notes": [], "analysis": ""}
        yield ev(type="file_start", target=target)

        if sec is None:
            r["status"] = "no-section"
            r["p4"].append("✗ no matching hunk in the patch for this file")
            yield ev(type="file_p4", target=target, line=r["p4"][-1], ok=False)
            results.append(r)
            yield ev(type="file_done", **_fslim(r))
            continue

        # ---- p4 prepare: map -> revert -> sync -> edit -c CL ----
        local = target
        if use_p4:
            local, mmsgs = P4.ensure_mapped(target, auto_map)
            for m in mmsgs:
                r["p4"].append(m)
                yield ev(type="file_p4", target=target, line=m, ok="✗" not in m)
            if not local:
                r["status"] = "not-mapped"
                results.append(r)
                yield ev(type="file_done", **_fslim(r))
                continue
            for verb, fn in (("revert", P4.revert), ("sync", P4.sync)):
                ok, msg = fn(target)
                line = f"{'✓' if ok else '·'} {verb:6s} {msg}"
                r["p4"].append(line)
                yield ev(type="file_p4", target=target, line=line, ok=ok)
            if cl:
                ok, msg = P4.edit(target, cl)
                line = f"{'✓' if ok else '✗'} edit   {msg}  (CL {cl})"
                r["p4"].append(line)
                yield ev(type="file_p4", target=target, line=line, ok=ok)

        if not os.path.isfile(local):
            r["status"] = "missing"
            r["p4"].append(f"✗ local file not found: {local}")
            yield ev(type="file_p4", target=target, line=r["p4"][-1], ok=False)
            results.append(r)
            yield ev(type="file_done", **_fslim(r))
            continue

        # ---- locate ----
        before = _read_text(local)
        cur_lines = before.split("\n")
        notes = core.locate_all(cur_lines, sec)
        r["notes"] = [_noteslim(nt) for nt in notes]
        for nt in notes:
            if nt.get("found"):
                cert = ("high" if nt["confident"]
                        else "low/tie" if nt.get("ambiguous") else "medium")
                where = (f"lines {nt['start_1']}-{nt['end']} "
                         f"({nt['confirmed']}/{nt['signal']} confirmed, {cert}; "
                         f"anchor `{nt['anchor']}` @ {nt['anchor_line']})")
            else:
                where = "from surrounding code (no anchor matched)"
            yield ev(type="located", target=target, hunk=nt["idx"], where=where)

        # ---- analyse (short) ----
        if core.claude_available():
            an = core.ask_claude(core.ANALYSE_PROMPT.format(patch=patch_text[:6000]),
                                 model)
            if an:
                r["analysis"] = an.strip()
                yield ev(type="analysis", target=target, text=r["analysis"])

        # ---- merge (Claude, streamed) ----
        yield ev(type="merge_start", target=target)
        buf = {"n": 0}

        def on_text(t, _t=target):
            buf["n"] += len(t)

        merged = core.merge_file_text(before, patch_text, sec["path"], notes,
                                      model, on_text=on_text)
        if not merged:
            r["status"] = "merge-failed"
            r["p4"].append("✗ Claude did not return a usable merge")
            results.append(r)
            yield ev(type="file_done", **_fslim(r))
            continue

        r["before"], r["after"] = before, merged
        if merged.strip() == before.strip():
            r["status"] = "no-change"
        else:
            r["status"] = "merged"
            _write_text(local, merged)
            r["diff"] = core.unified_diff(before, merged, sec["path"])
            if use_p4:
                r["diff"] = P4.diff(target) or r["diff"]
        results.append(r)
        yield ev(type="file_done", **_fslim(r))

    # ---- shelve == the SCL ----
    shelved = False
    merged_any = any(r["status"] == "merged" for r in results)
    if use_p4 and cl and do_shelve and merged_any:
        ok, msg = P4.shelve(cl)
        shelved = ok
        yield ev(type="log", level="info" if ok else "err", msg="p4 shelve: " + msg)

    yield ev(type="done", ok=merged_any, cl=cl, shelved=shelved,
             description=description,
             summary={"merged": sum(r["status"] == "merged" for r in results),
                      "no_change": sum(r["status"] == "no-change" for r in results),
                      "failed": sum(r["status"] not in ("merged", "no-change")
                                    for r in results)},
             files=[_fslim(r) for r in results])


def _fslim(r):
    return {k: r[k] for k in ("target", "status", "p4", "before", "after",
                              "diff", "notes", "analysis")}


def _noteslim(n):
    keys = ("idx", "found", "start_1", "end", "confirmed", "signal", "anchor",
            "anchor_line", "confident", "ambiguous", "gap")
    return {k: n[k] for k in keys if k in n}


def _read_text(path):
    with open(path, "rb") as fh:
        data = fh.read()
    return data.replace(b"\r\n", b"\n").decode("utf-8", "replace")


def _write_text(path, text):
    # preserve CRLF if the file had it
    with open(path, "rb") as fh:
        had_crlf = b"\r\n" in fh.read()
    data = text.encode("utf-8")
    if had_crlf:
        data = data.replace(b"\n", b"\r\n")
    with open(path, "wb") as fh:
        fh.write(data)


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self._send(200, json.dumps({
                "p4": P4.have_p4(), "claude": core.claude_available()}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/run":
            self._send(404, json.dumps({"error": "not found"}))
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, json.dumps({"error": "bad json"}))
            return

        files = [f.strip() for f in data.get("files", []) if f.strip()]
        patch_text = data.get("patch_text", "")
        if not patch_text and data.get("patch_path"):
            try:
                with open(os.path.expanduser(data["patch_path"]),
                          encoding="utf-8", errors="replace") as fh:
                    patch_text = fh.read()
            except OSError as exc:
                self._send(400, json.dumps({"error": f"patch: {exc}"}))
                return
        if not files or not patch_text:
            self._send(400, json.dumps({"error": "need files[] and a patch"}))
            return

        # stream NDJSON as the job runs
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        gen = run_job(files, patch_text,
                      description=data.get("description") or None,
                      model=data.get("model") or None,
                      auto_map=data.get("auto_map", True),
                      use_p4=data.get("use_p4", True),
                      do_shelve=data.get("shelve", True))
        try:
            for event in gen:
                self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                self.wfile.flush()
        except BrokenPipeError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="p4-merger web app")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"p4-merger  ->  {url}")
    print(f"p4:     {'found' if P4.have_p4() else 'NOT found (use the no-p4 toggle)'}")
    print(f"claude: {'found' if core.claude_available() else 'NOT found'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
