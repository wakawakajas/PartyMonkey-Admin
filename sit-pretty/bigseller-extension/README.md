# Sit Pretty for BigSeller (Chrome extension)

Puts a camera button beside every Shopee order number on BigSeller pages.

- Faded button: Sit Pretty has no video for that order yet.
- Dark button with a number: that many videos are saved for the order.
- Click it: a small window opens with three views. Close it with its X (or Esc).
  - **Simple view**: the raw packing clip. If there is none, tick the table and press
    *Get packing clip* (the "Scan to ship" time is read from BigSeller automatically, no Claude).
    Then *Add simple count* asks Claude (Sonnet) for a light ITEMS PACKED counter video.
  - **Detailed packing**: the fully labelled packing video, or *Make detailed video*.
  - **Unboxing**: the unboxing recording and labelled unboxing videos.
  - Every video: speed 1x-16x, back/forward 10 s, Download.

It only reads BigSeller (order search and Order Log). It never clicks anything there.

## Install on a shop PC (once)

1. Chrome: open `chrome://extensions`, turn on **Developer mode** (top right).
2. **Load unpacked**, pick this folder (`print-template-builder\sit-pretty\bigseller-extension`).
3. Click the puzzle icon, pin **Sit Pretty for BigSeller**, click it, type the Sit Pretty PIN,
   press **Save and test**. The address stays `DESKTOP-ABGC9KO:8787` unless Sit Pretty runs on another PC.
4. Reload BigSeller.

After `git pull` brings a newer version, press the reload arrow on the extension's card in
`chrome://extensions`.
