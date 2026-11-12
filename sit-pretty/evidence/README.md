# Packing evidence videos (made by Claude Code)

Staff ask for a video in Sit Pretty (**Evidence video** page): they pick the return, tick the
camera(s) that filmed the packing and, if they know it, BigSeller's "Scan to ship" time.
That writes `N:\Yong Chuan\Dispute\<OrderID>\evidence-request.json` with `"status": "queued"`.
The hourly Claude Code task `sit-pretty-evidence-videos` picks these up.

## Returns list refresh

If `N:\Yong Chuan\Dispute\_sitpretty\refresh-request.json` exists, staff pressed "Refresh from Shopee".
Rebuild `_sitpretty\returns.json` exactly as the daily task `shopee-return-claims-refresh` does (open
returns with items, due, todo and tracking numbers, read from Seller Centre in Claude in Chrome), then
delete `refresh-request.json`. Do this before making videos.
Include the customer's words (`buyer_text`, `buyer_videos`, `buyer_photos`) and Shopee's latest request
(`shopee_ask`, `shopee_ask_at`), as described in that task.

## Unboxing videos (unboxing-video-request.json)

After Stop and save, staff can ask for a labelled copy of a return's unboxing. That writes
`<OrderID>\unboxing-video-request.json` (`"kind": "unboxing"`, `"status": "queued"`) with:
`video` (the recording in the same folder), `cams`/`areas` (camera and its table area), and what to show:
`count` (big **ITEMS RECEIVED n / N** counter, N = what the order says was returned), `highlight_items`
(name tag + arrow on each item as it comes out), `highlight_weight` (zoom into the scale reading and hold
it 2 s), `missing` + `missing_text` (a clear banner such as "ONLY 1 OF 2 PUMPS RETURNED"), and `note`.

Make it the same way as a packing video, with these differences:
- No memory-card download: the source is `video` itself. Contact-sheet it, find each item coming out.
- Spec: `counter.label` = "ITEMS RECEIVED"; use `banners` / `callouts` for missing, used or wrong items
  (here showing what is wrong **helps** us, so say it plainly and in big words); use the `waybill`
  zoom for the return waybill, and a second zoom-and-freeze on the scale when `highlight_weight` is on.
- Use findings.json (below) and the customer's words in returns.json to know what to point at.
- Basename `<OrderID>_unboxing_labelled`; set the request to done/problem exactly like packing requests.
- **Name what is missing.** Compare the order's items with what comes out and say it by name in the
  banner, e.g. "MATTE HONEY NOT RETURNED" (or "ONLY 1 OF 2 RETURNED: MATTE HONEY MISSING"), not just a count.
- **Every arrow tip must touch its item.** Before rendering, draw the planned arrow tips and boxes on the
  frame at that moment (with the 0.1 grid) and look: the tip sits on the item itself, not next to it
  (bubble wrap, box, hands). Check again on the rendered video; move it if it is off.
- **Waybill close-up must be readable.** Pick the frame where the waybill is closest to the camera and
  sharpest (score several with an edge filter, not just the first second). If no frame shows readable
  text, leave the waybill zoom out and rely on the info strip and end card for the order ID.

## What staff found (findings.json)

After an unboxing, staff tick what they saw in Sit Pretty or Pigu (item used, seal opened, items missing,
wrong item, damaged by buyer, dirty, buyer's photo not our item, came back fine) and add a note. It is
saved as `<OrderID>indings.json`. Use it when writing the appeal text and when choosing which unboxing
moments to point at.

## For each queued request (Claude)

1. Set `"status": "working"` in the request file.
2. **Packing time.** Use `scan_time` from the request if given. Otherwise read the order in
   BigSeller (Chrome, All Orders, search the order number, Order Details, Order Log): the time on
   "Scan to ship on APP" is when packing finished. Packing starts after the previous order's ship
   scan on the same table; if you cannot tell, take 15 minutes before.
3. **Footage.** For each camera in `cams`, list that day's memory-card recordings
   (`.venv\Scripts\python.exe sdclip.py list <ip> <YYYYMMDD>`) and download from the packing start
   to 2 minutes after the ship scan (`sdclip.py get <ip> <start epoch> <end epoch> <out.mp4>`).
   Pieces are 3 minutes long; download each and join them in order (ffmpeg concat). Keep going
   past the ship scan until the parcel leaves the table (the FRAGILE sticker often goes on last).
4. **Items.** Take the order's lines from Seller Centre or BigSeller (name, variation, qty).
   Count what the order says, never guess from the picture. Number them 1, 2, 3 in that order.
   Items whose design cannot be read (folded foil balloons) are "Item 1", "Item 2"...
5. **Watch the footage.** Make contact sheets (1 frame per second, cropped to the table area from
   Sit Pretty's Cameras page, with the second printed on each frame) and look at them. Find:
   the label in the brown label box (top-left of the mat) or on the parcel; each item lying flat;
   the moment each item goes into the parcel; any counting (balloons, sheets) and how many per
   step; bubble wrap; taping; polymailer; FRAGILE sticker; waybill.
   Check positions on a frame with a 0.1 grid before placing any box or arrow.
6. **Write the spec** (`<OrderID>\evidence-spec-<camera>.json`, format below) and run
   `.venv\Scripts\python.exe evidence\render.py <spec>`. It writes
   `<OrderID>_packing_<camera>_video1of2.mp4` ... into the order folder.
7. **Check** 4-6 frames of the result (item tags on the right things, counter right, nothing
   covering the brown label box, waybill readable). Fix the spec and render again if needed.
8. Set `"status": "done"`, `"videos": [file names]`, `"done_at"`, and a one-line `"summary"`.
   If something stops you (no footage on the card, camera offline), set `"status": "problem"`
   and `"problem"` to a plain sentence for staff.

## Rules (from the owner)

- **Never cover the action.** Words, boxes and arrows must not sit over the hands, the parcel or the item
  being handled. Banners are placed automatically where nothing moves; give every tag and callout
  `"label_at": "auto"` (it picks the quietest spot near `arrow_to`) unless you have checked a frame.
  If render.py prints a WARNING about a label over movement, fix it and render again.
- **Keep it calm: one label per item at a time.** Don't chase a moving item with a string of short
  callouts. Tag it once as it comes out or goes in (one callout, 3-5 s), then again when it lies still
  (`flat`). Never show the same item's name twice at once. A callout pointing at a place (e.g. the empty
  box) ends before something else moves into that place.
- Only show what helps us. **Never** show a negative ("not seen", "missing"): if something cannot
  be seen, leave it out of the video.
- Every item gets a name tag and an arrow, when flat on the table and again when it goes in.
- The ITEMS PACKED counter always shows (top-right). Counting (100 latex balloons by 1s or 2s,
  glue dot sheets) gets a sub-counter that steps with each count, at normal speed.
- Taping: just the banner text ("TAPED TIGHTLY so ... cannot open"). No moving arrow.
- Polymailer: arrows may follow the mailer (`tracked`, target `pink`).
- Normal speed for counting, items going in, waybill and FRAGILE sticker; 4x for the rest.
- Nothing may cover the brown label box (top-left of the mat, about x 0.15-0.25, y 0.15-0.40).
- Order ID, date/time and VIDEO n / N sit together bottom-right (the renderer does this).
- End with the zoom into the waybill and the 2.5 s freeze (renderer does this from `waybill`).

## Spec format

Fractions (0-1) of the cropped table view; times in seconds from the first frame of `source`.

```json
{
  "order": "2611112RWJWT6H",
  "camera": "pack-183",
  "source": "path/to/joined_4k.mp4",
  "table_area": {"x": 0.22, "y": 0.24, "w": 0.64, "h": 0.68},
  "start_epoch": 1794451406,
  "date_text": "12 Nov 2026",
  "output_dir": "N:/Yong Chuan/Dispute/2611112RWJWT6H",
  "basename": "2611112RWJWT6H_packing_pack-183",
  "segments": [[0, 18, 1], [18, 93, 4], [93, 140, 1]],
  "flat": {"start": 7, "end": 17.5, "tags": [
      {"text": "1  Black/Wine wrapper", "box": [0.45, 0.26, 0.35, 0.33], "label_at": [0.60, 0.70], "arrow_to": [0.66, 0.625]}]},
  "counter": {"label": "ITEMS PACKED", "total": 5, "steps": [[30, 2], [112.5, 3]]},
  "subcounts": [{"text": "4  GLUE DOT SHEETS", "total": 5, "steps": [94.4, 95.2, 96.0, 96.8, 97.6], "end": 99,
                 "box": [0.40, 0.15, 0.24, 0.27], "label_at": [0.37, 0.44]}],
  "banners": [{"text": "TAPED TIGHTLY so the bubble wrap cannot open", "color": "blue", "start": 50, "end": 86}],
  "callouts": [{"text": "FRAGILE STICKER APPLIED", "color": "green", "start": 182.6, "end": 999,
                "label_at": [0.26, 0.61], "arrow_to": [0.54, 0.32]}],
  "tracked": [{"text": "3  Twine x2 into POLYMAILER", "color": "pink", "target": "pink", "start": 106, "end": 113}],
  "waybill": {"zoom_frame_t": 183.4, "sharp_t": 180.9, "center": [0.7135, 0.4907], "width": 0.1458, "rotate": 180},
  "card_lines": ["SPX Express", "Packed 12 Nov 2026  10:43-10:46", "5 / 5 items packed", "Bubble wrap + taped tightly"]
}
```

`waybill.center` and `width` are fractions of the **full** camera frame (not the table view).
`rotate` turns the frozen close-up so the order ID reads left to right (0, 90, 180, 270).
`sharp_t`: pick the sharpest frame while the label is still (score a few with an edge filter).
`label_at` may be `"auto"` for flat tags and callouts. Banners take an optional `"at": [x, y]`; without it
they go where nothing moves. `"keep_clear": [[x, y, w, h], ...]` (fractions of the table view) are areas
nothing may cover, e.g. the brown label box on the packing mat.
Colours: yellow, orange, green, blue, pink. The last segment's end is where the video stops.
