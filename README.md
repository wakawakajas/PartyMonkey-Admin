# Print Template Builder

A single-page, mobile-friendly tool for building double-sided print templates
(name cards, labels, etc.), laying out a card grid on a sheet, generating
cutting reference marks automatically, and exporting front/back print-ready
PDFs.

It's one self-contained file: `index.html`. No build step, no dependencies to
install.

## Running it

Just open `index.html` in a browser. For the most reliable experience
(especially PDF uploads and the enlarge/zoom view), serve it over a local
server rather than opening the file directly:

```bash
npx serve .
# or
python3 -m http.server 8000
```

Then visit the printed local URL.

## What it does

- **Sheet size** — set the full sheet width/height/unit (mm, in, or px).
- **Layout & placeholders** — set a finished card size and a columns × rows
  grid, choose Manual or Center layout, set lead/side trim and gutter
  spacing, and generate the placeholder grid. Placeholders can also be
  bulk-edited or fine-tuned individually.
- **Cutting reference line** — auto-generated corner marks at every corner of
  every placeholder, offset outward by a configurable distance, marking each
  card's actual width and height. Always the bottom layer, included in the
  exported PDF.
- **Cut line overlay** — a separate, simpler placeholder outline in red or
  white, preview-only (not exported), for quickly checking alignment.
- **Final artwork** — upload a front/back image or PDF, repeated into every
  placeholder, centered, with independently adjustable width/height and
  optional bleed on all sides.
- **Order ID** — small text baked into the bottom-left corner of both the
  preview and the exported PDF.
- **Template library** — save/load/delete named templates (sheet size,
  layout, placeholders, cut-line and reference-mark settings).
- **Export** — bundles front + back as two PDFs inside a single
  `orderid_pdfs.zip` download (bundling avoids browsers silently blocking a
  second simultaneous file download).

## Implementation notes

- **No external PDF library for export.** The PDF and ZIP writers are
  hand-rolled directly in the script (`buildPdfBytes`, `buildZip`) so export
  has no CDN dependency and can't fail because a script didn't load.
- **PDF *upload* still needs a CDN.** Reading an uploaded PDF (for a base
  template or final artwork) uses `pdf.js` loaded from cdnjs. If that CDN is
  blocked, PDF uploads will fail with a clear error but JPG/PNG uploads are
  unaffected.
- **Template storage.** Templates are saved through `window.storage`, which
  is normally a Claude.ai-artifact-only API. This file includes a small
  polyfill near the top of the `<script>` block that falls back to the
  browser's own `localStorage` when `window.storage` isn't present — so
  saved templates work here too, but they're local to whichever browser/
  device you're using (no server, no sync across devices/browsers).
- **Everything is client-side.** Uploaded images are held as data URLs in
  memory/localStorage; nothing is uploaded anywhere.

## Ideas for continuing this in Claude Code

- Swap the `localStorage` polyfill for a real backend (e.g. a small
  Node/SQLite or Supabase service) if templates need to sync across devices.
- Split `index.html` into separate JS/CSS files and add a lightweight build
  step if the project keeps growing.
- Add automated tests for the PDF/ZIP writers (there's already a pattern for
  this — see the manual Node.js + Python `pypdf`/`zipfile` validation used
  while building it).
- Package as an installable PWA for offline use on a shop floor tablet.
