# print-template-builder

## Supabase: every new table needs grants

From 2026-10-30 Supabase no longer grants Data API access to new tables in
`public` automatically. A table created without grants answers every
supabase-js call with "permission denied" (42501).

Any SQL file that runs `create table public.<name>` must grant it in the same
file, right after the table (and its RLS policies):

```sql
grant select on public.<name> to anon;
grant select, insert, update, delete on public.<name> to authenticated;
grant select, insert, update, delete on public.<name> to service_role;
```

Drop the `anon` line for tables only signed-in users should read. Existing
tables keep the grants they already have.

## Two computers push here: pull first, never force

Pigu is edited from more than one computer (the Admin PC and the main
computer). Before changing anything, run `git pull` and work on top of
`origin/main`. Never `git push --force` (or `--force-with-lease`): a rejected
push means the other computer has pushed, so pull, merge, and push again.

## Print colour pipeline (changed 2026-11-03)

On 3 Nov the Admin PC rebuilt how Pigu makes colour for print (commits up to
8c4ffe8). Before touching the print preview or the export colour code, read
`C:\NAS Folders\Partymonkey NAS\Claude Handoff\Print Colour Tuning\`
(`HANDOFF-main-computer-2026-11-03.md`, then `print-colour-tuning.md`):

- Every export is CMYK only (GRACoL2013), matched to the measured Fiery gloss
  and matte papers (`profiles/`); nothing goes through the Fiery's RGB source.
- Print preview: File colours / As printed (simulates the paper, as
  Photoshop's Simulate Paper Color with `profiles/pdf5-<paper>/`).
- Printer warmth, Hue/Saturation and Brightness (`printLook`, in Lab) apply
  to the export and As printed; greys stay K-only, white stays paper.
- Test sheet: the design in numbered versions on one sheet, then Use #n.
- Sliders re-proof a kept drawing (`pcReproof`, `canvas._pcPre`/`_pcRaw`);
  the exact pass runs on release.
- `index.html` mixes CRLF and LF line endings: edit it byte-exact (no
  `sed -i`, which rewrote every line once).
