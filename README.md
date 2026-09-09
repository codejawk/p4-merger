# p4-merger

A small web app that merges a vendor patch into Perforce-managed files and hands
you back a **shelved changelist (SCL)** — never a submit.

Give it one or more files and a patch. It will:

1. **Map** each file into your client workspace (auto-mapping it into your client
   `View` if it isn't already, when you allow it).
2. **Revert** any pending edits and **sync** to the latest revision.
3. Create one **pending changelist** and open the files into it (`p4 edit -c`).
4. Use the local **`claude` CLI** to analyse the patch and merge it into each
   file — locating each change by *content*, not by the patch's (unreliable)
   line numbers.
5. **Shelve** the changelist and return the CL number — the SCL you can review
   with `p4 describe -S <cl>` and share or `p4 unshelve` elsewhere.

It never runs `p4 submit`. The shelved changelist is yours to review and submit.

## How the merge works (no line-number trust)

Vendor patches are written against a different revision, so `@@` line numbers are
usually wrong. Instead of applying mechanically, it works like a person:

- **Anchor** on the most distinctive single line of each hunk (a function
  signature, a specific `if (...)`), trimmed of surrounding whitespace.
- **Confirm** the spot with the rest of the hunk's context / removed lines.
- **Tie-break** with the declared line number only when context is ambiguous —
  and it flags those cases as low-confidence.

Then Claude edits the file with that located region as a hint, changing only what
the patch requires and keeping everything else byte-for-byte identical.

## Requirements

- Python 3 (stdlib only — no pip installs)
- The [`claude`](https://claude.com/claude-code) CLI on `PATH`
- `p4` on `PATH` for the Perforce steps (optional — see dry-run below)

## Run

```bash
cd p4-merger
python3 server.py          # then open http://127.0.0.1:8770
python3 server.py --port 9000
```

## Dry run without Perforce

Tick **"no p4 — merge local files only"** in the UI (or it auto-selects when `p4`
isn't found). Put local file paths in the files box; it locates + merges them in
place and shows the diff, skipping every p4 step. Good for trying the merge
before you're at a machine with `p4`.

## Files

| file | role |
|------|------|
| `server.py` | web server + the orchestration flow; streams progress as NDJSON |
| `core.py`   | patch parsing, content-based location, Claude merge |
| `p4.py`     | Perforce commands: where / auto-map / revert / sync / change / edit / shelve |
| `index.html`| the single-page UI |

## Safety

- **Never** `p4 submit`. The result is always a shelved (or at most pending) CL.
- Auto-mapping only **appends** a line to your client `View`; it never removes
  mappings, and it's off unless you leave the toggle on.
- The merge changes only the patched region; review the diff (and `p4 diff`)
  before you submit.
