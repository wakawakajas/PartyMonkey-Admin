"""Render a Shopee-ready, labelled packing evidence video from a spec.

Usage:  .venv\\Scripts\\python.exe evidence\\render.py <spec.json>

Claude (or a person) watches the footage and writes the spec: what is on the table,
when each item goes in, counting, taping, the waybill. This script always draws it
the same way:
  * one black info box bottom-right: ORDER id, date + running clock, VIDEO n / N
  * item name tags with arrows while items lie flat; a big ITEMS PACKED n / N counter
  * sub-counters (e.g. GLUE DOT SHEETS 3 / 5) while something is counted
  * arrows that follow the tape gun (yellow) or the polymailer (pink)
  * normal speed where it matters, FAST FORWARD 4x elsewhere (from "segments")
  * zoom into the waybill, freeze 2.5 s, then a summary card
  * split into parts of at most 58 s, each under Shopee's 30 MB, no audio
Never draws negative findings: the spec only lists what was seen.

Spec coordinates are fractions (0-1) of the cropped table view; times are seconds
from the first frame of "source". See evidence/README.md for the full format.
"""
import json, math, os, subprocess, sys, tempfile, shutil
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from server import FFMPEG  # noqa: E402  same ffmpeg search as Sit Pretty

ARIAL = r"C:\Windows\Fonts\arialbd.ttf"
FFONT = "C\\:/Windows/Fonts/arialbd.ttf"
W, H = 1280, 766
F = lambda n: ImageFont.truetype(ARIAL, n)
BOLD, MID, BIG, NUM, ENDF, HUGE, CNT = F(36), F(30), F(48), F(40), F(56), F(78), F(44)
COL = {"yellow": (255, 214, 0), "orange": (255, 170, 0), "green": (36, 160, 70), "blue": (30, 107, 255),
       "pink": (214, 51, 132), "dark": (16, 16, 16), "black": (0, 0, 0), "white": (255, 255, 255)}
YEL, ORG, GRN, DRK, BLK, WHT = COL["yellow"], COL["orange"], COL["green"], COL["dark"], COL["black"], COL["white"]
ZOOM_S, FREEZE_S, CARD_S, MAX_PART = 1.5, 2.5, 3, 58


def run(cmd, timeout=3000):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise SystemExit("ffmpeg failed: " + r.stderr[-1500:])


def P(xy):
    return int(xy[0] * W), int(xy[1] * H)


def tw(d, text, font):
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    return r - l


def label(d, text, xy, bg, fg=BLK, font=BOLD):
    x, y = xy
    l, t, r, b = d.textbbox((x, y), text, font=font)
    d.rounded_rectangle((l - 12, t - 8, r + 12, b + 8), 10, fill=bg + (242,), outline=WHT + (255,), width=3)
    d.text((x, y), text, font=font, fill=fg)
    return (l - 12, t - 8, r + 12, b + 8)


def arrow(d, a, b, color):
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    head = [b, (b[0] - 34 * math.cos(ang - 0.45), b[1] - 34 * math.sin(ang - 0.45)),
            (b[0] - 34 * math.cos(ang + 0.45), b[1] - 34 * math.sin(ang + 0.45))]
    end = (b[0] - 26 * math.cos(ang), b[1] - 26 * math.sin(ang))
    d.line([a, end], fill=WHT + (255,), width=14); d.polygon(head, fill=WHT + (255,))
    d.line([a, end], fill=color + (255,), width=8)
    d.polygon([(b[0] - 4 * math.cos(ang), b[1] - 4 * math.sin(ang))] + head[1:], fill=color + (255,))


def edge_point(rect, target):
    """Where a line from the label's centre towards the target leaves the label."""
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = target[0] - cx, target[1] - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (x1 - x0) / 2 / abs(dx) if dx else 1e9
    sy = (y1 - y0) / 2 / abs(dy) if dy else 1e9
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


def tag(d, text, at, to=None, color=YEL, fg=BLK, font=BOLD):
    rect = label(d, text, P(at), color, fg, font)
    if to:
        tp = P(to)
        arrow(d, edge_point(rect, tp), tp, color)
    return rect


def box(d, b, color=YEL):
    x0, y0 = P((b[0], b[1])); x1, y1 = P((b[0] + b[2], b[1] + b[3]))
    d.rectangle((x0, y0, x1, y1), outline=WHT + (255,), width=9)
    d.rectangle((x0, y0, x1, y1), outline=color + (255,), width=5)


class Builder:
    def __init__(self, spec, work):
        self.s, self.work, self.n = spec, work, 0

    def layer(self, draw):
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw(ImageDraw.Draw(im))
        self.n += 1
        p = os.path.join(self.work, f"L{self.n:03d}.png"); im.save(p); return p


def track(src, area, windows, work):
    """Centre of the moving yellow (tape gun) / pink (polymailer) blob, 4 times a second."""
    import numpy as np
    lo = min(w["start"] for w in windows); hi = max(w["end"] for w in windows)
    tdir = os.path.join(work, "trk"); os.makedirs(tdir, exist_ok=True)
    run([FFMPEG, "-v", "error", "-y", "-ss", str(lo), "-i", src, "-t", str(hi - lo), "-vf",
         f"fps=4,crop=iw*{area['w']}:ih*{area['h']}:iw*{area['x']}:ih*{area['y']},scale=320:-2", os.path.join(tdir, "t%05d.png")])
    files = sorted(f for f in os.listdir(tdir) if f.endswith(".png"))
    hsv = [np.asarray(Image.open(os.path.join(tdir, f)).convert("HSV")).astype(int) for f in files]
    ranges = {"yellow": (28, 48, 150, 170), "pink": (215, 245, 90, 170)}
    masks = {}
    for k, (h0, h1, smin, vmin) in ranges.items():
        m = np.array([(a[..., 0] > h0) & (a[..., 0] < h1) & (a[..., 1] > smin) & (a[..., 2] > vmin) for a in hsv])
        masks[k] = m & ~(m.mean(0) > 0.45)          # drop things of that colour that never move
    Hh, Ww = hsv[0].shape[:2]
    out = {}
    for i in range(len(files)):
        t = round(lo + i / 4, 2); out[t] = {}
        for k, m in masks.items():
            mm = m[i].copy(); mm[:, : int(0.1 * Ww)] = False; mm[:, int(0.86 * Ww):] = False
            best = None
            for cy in range(0, Hh, 10):
                for cx in range(0, Ww, 10):
                    c = mm[max(0, cy - 25):cy + 25, max(0, cx - 25):cx + 25].sum()
                    if best is None or c > best[0]:
                        best = (c, cx, cy)
            if best and best[0] >= 60:
                c, cx, cy = best
                sub = mm[max(0, cy - 30):cy + 30, max(0, cx - 30):cx + 30]; ys, xs = sub.nonzero()
                out[t][k] = ((xs.mean() + max(0, cx - 30)) / Ww, (ys.mean() + max(0, cy - 30)) / Hh)
    return out


def render(spec_path):
    s = json.load(open(spec_path, encoding="utf8"))
    src, area, order = s["source"], s["table_area"], s["order"]
    out_dir = s["output_dir"]; os.makedirs(out_dir, exist_ok=True)
    base = s.get("basename", f"{order}_packing_evidence")
    work = tempfile.mkdtemp(prefix="sp_render_")
    B = Builder(s, work)
    segs = s["segments"]
    src_len = segs[-1][1]
    layers = []                                                       # (png, start, end)

    INF = F(28)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    o_txt = f"ORDER {order}"
    w_order = tw(probe, o_txt, INF); w_clock = tw(probe, s.get("date_text", "") + "  00:00:00", INF); w_vid = tw(probe, "VIDEO 9 / 9", INF)
    x0 = W - 14 - (w_order + w_clock + w_vid + 4 * 22 + 2 * 20)
    xo, xc, xv = x0 + 22, x0 + 22 + w_order + 42, x0 + 22 + w_order + 42 + w_clock + 42
    STRIP_Y = H - 52

    def info(d):
        d.rounded_rectangle((x0, STRIP_Y - 8, W - 14, H - 10), 10, fill=(0, 0, 0, 235), outline=(255, 255, 255, 255), width=2)
        d.text((xo, STRIP_Y), o_txt, font=INF, fill=WHT)
        d.text((xc - 24, STRIP_Y), "|", font=INF, fill=(150, 150, 150))
        d.text((xv - 24, STRIP_Y), "|", font=INF, fill=(150, 150, 150))
    layers.append((B.layer(info), 0, 99999))

    # ---- keep the words off the action: how much the picture changes in each spot, 4 times a second
    import numpy as np
    AW, AH = 128, 77
    adir = os.path.join(work, "act"); os.makedirs(adir, exist_ok=True)
    run([FFMPEG, "-v", "error", "-y", "-i", src, "-t", str(src_len), "-vf",
         f"fps=4,crop=iw*{area['w']}:ih*{area['h']}:iw*{area['x']}:ih*{area['y']},scale={AW}:{AH},format=gray",
         os.path.join(adir, "a%05d.png")])
    frs = [np.asarray(Image.open(os.path.join(adir, f)), dtype=np.int16) for f in sorted(os.listdir(adir))]
    act = np.zeros((max(1, len(frs)), AH, AW), np.float32)
    for i in range(1, len(frs)):
        m = Image.fromarray(((np.abs(frs[i] - frs[i - 1]) > 14) * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))
        act[i] = np.asarray(m, np.float32) / 255
    taken = [((x0 - 4, STRIP_Y - 12, W, H), 0, 99999)]           # the info strip
    cnt_ = s.get("counter")
    if cnt_:
        ct = f"{cnt_.get('label', 'ITEMS PACKED')}  {cnt_['total']} / {cnt_['total']}"
        taken.append(((W - tw(probe, ct, CNT) - 50, 0, W, 76), 0, 99999))
    keep = [(int(k[0] * W), int(k[1] * H), int((k[0] + k[2]) * W), int((k[1] + k[3]) * H)) for k in s.get("keep_clear", [])]

    def hit(a, b):
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])

    def busy(rect, t0, t1):
        x0_, y0_, x1_, y1_ = rect
        if x0_ < 0 or y0_ < 56 or x1_ > W or y1_ > H:
            return 1e9
        i0 = max(0, int(t0 * 4) - 2); i1 = min(len(act), int(t1 * 4) + 3)
        ax0, ay0 = int(x0_ * AW / W), int(y0_ * AH / H)
        a = act[i0:i1, ay0:max(ay0 + 1, int(y1_ * AH / H)), ax0:max(ax0 + 1, int(x1_ * AW / W))]
        sc = float(a.sum()) / max(1, i1 - i0)
        sc += sum(1e6 for (rc, a0, a1) in taken if a0 < t1 and t0 < a1 and hit(rect, rc))
        sc += sum(1e6 for k in keep if hit(rect, k))
        return sc

    def rect_of(text, xy, font=BOLD):
        l, t_, r_, b_ = probe.textbbox(xy, text, font=font)
        return (l - 12, t_ - 8, r_ + 12, b_ + 8)

    def place_banner(text, t0, t1):
        w = tw(probe, text, BOLD)
        cands = [((W - w) // 2, 62)] + [(x, y) for y in (62, 120, H - 136, H - 196) for x in (26, (W - w) // 2, W - w - 26)]
        best = min(cands, key=lambda p: busy(rect_of(text, p), t0, t1) + 0.02 * abs(p[1] - 62) / H)
        taken.append((rect_of(text, best), t0, t1))
        return best

    def place_tag(text, to, t0, t1):
        """Label for an arrow: the quietest spot 120-330 px away from what it points at."""
        tx, ty = P(to); w = tw(probe, text, BOLD)
        best, bs = None, None
        for dist in (130, 200, 270, 330):
            for k in range(12):
                ang = k * math.pi / 6
                cx, cy = tx + dist * math.cos(ang), ty + dist * math.sin(ang)
                xy = (int(cx - w / 2), int(cy - 20))
                sc = busy(rect_of(text, xy), t0, t1) + dist / 2000
                if bs is None or sc < bs:
                    best, bs = xy, sc
        taken.append((rect_of(text, best), t0, t1))
        return (best[0] / W, best[1] / H)

    def check_tag(text, at, t0, t1):
        rc = rect_of(text, P(at))
        sc = busy(rc, t0, t1) - sum(1e6 for (q, a0, a1) in taken if a0 < t1 and t0 < a1 and hit(rc, q))
        if sc > 0.15 * (rc[2] - rc[0]) * (rc[3] - rc[1]) * AW * AH / (W * H):
            print(f"WARNING: '{text}' sits over movement at {t0}-{t1}s; use \"label_at\": \"auto\" or move it",
                  file=sys.stderr)
        taken.append((rc, t0, t1))

    flat = s.get("flat")
    if flat:
        for t_ in flat["tags"]:
            if t_.get("label_at") == "auto" and t_.get("arrow_to"):
                t_["label_at"] = place_tag(t_["text"], t_["arrow_to"], flat["start"], flat["end"])
            else:
                check_tag(t_["text"], t_["label_at"], flat["start"], flat["end"])

        def fl(d):
            for t_ in flat["tags"]:
                if t_.get("box"):
                    box(d, t_["box"])
                tag(d, t_["text"], t_["label_at"], t_.get("arrow_to"))
        layers.append((B.layer(fl), flat["start"], flat["end"]))

    cnt = s.get("counter")
    if cnt:
        steps = [[0, 0]] + cnt["steps"]
        for i, (t0, n) in enumerate(steps):
            t1 = steps[i + 1][0] if i + 1 < len(steps) else 99999
            def cf(d, n=n):
                txt = f"{cnt.get('label', 'ITEMS PACKED')}  {n} / {cnt['total']}"
                label(d, txt, (W - tw(d, txt, CNT) - 26, 12), GRN if n == cnt["total"] else ORG,
                      WHT if n == cnt["total"] else BLK, CNT)
            layers.append((B.layer(cf), t0, t1))

    for sc in s.get("subcounts", []):
        st = sc["steps"]
        for k, t0 in enumerate(st, 1):
            t1 = st[k] if k < len(st) else sc.get("end", t0 + 1.5)
            def sf(d, k=k, sc=sc):
                if sc.get("box"):
                    x0, y0 = P((sc["box"][0], sc["box"][1])); x1, y1 = P((sc["box"][0] + sc["box"][2], sc["box"][1] + sc["box"][3]))
                    d.rounded_rectangle((x0, y0, x1, y1), 14, outline=WHT + (255,), width=10)
                    d.rounded_rectangle((x0, y0, x1, y1), 14, outline=YEL + (255,), width=6)
                label(d, f"{sc['text']}  {k} / {sc['total']}", P(sc["label_at"]), YEL, BLK, CNT)
            layers.append((B.layer(sf), t0, t1))

    for bn in s.get("banners", []):
        c = COL[bn.get("color", "blue")]
        at = P(bn["at"]) if bn.get("at") else place_banner(bn["text"], bn["start"], min(bn["end"], src_len))
        def bf(d, bn=bn, c=c, at=at):
            label(d, bn["text"], at, c, BLK if c in (YEL, ORG) else WHT)
        layers.append((B.layer(bf), bn["start"], bn["end"]))

    for co in s.get("callouts", []):
        c = COL[co.get("color", "green")]
        if co.get("label_at") == "auto" and co.get("arrow_to"):
            co["label_at"] = place_tag(co["text"], co["arrow_to"], co["start"], min(co["end"], src_len))
        elif co.get("label_at") != "auto":
            check_tag(co["text"], co["label_at"], co["start"], min(co["end"], src_len))
        def cof(d, co=co, c=c):
            if co.get("box"):
                box(d, co["box"], c)
            tag(d, co["text"], co["label_at"], co.get("arrow_to"), c, BLK if c in (YEL, ORG) else WHT)
        layers.append((B.layer(cof), co["start"], co["end"]))

    # tracked arrows (move with the tape gun / polymailer) via sendcmd
    tracked = s.get("tracked", [])
    cin, cmds = [], []
    if tracked:
        tr = track(src, area, tracked, work)
        keys = sorted(tr)
        for j, tk in enumerate(tracked):
            c = COL[tk.get("color", "blue")]
            probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
            im = Image.new("RGBA", (max(tw(probe, tk["text"], BOLD) + 40, 120), 170), (0, 0, 0, 0)); dd = ImageDraw.Draw(im)
            label(dd, tk["text"], (16, 10), c, BLK if c in (YEL, ORG) else WHT)
            arrow(dd, (im.width // 2, 62), (im.width // 2, 166), c)
            p = os.path.join(work, f"C{j}.png"); im.save(p); cin.append(p)
            tag_ = f"t{j}"; last = None
            for k in keys:
                if not (tk["start"] <= k < tk["end"]):
                    continue
                pos = tr[k].get(tk.get("target", "yellow")) or last
                last = pos or last
                if pos:
                    x = min(max(int(pos[0] * W - im.width / 2), 0), W - im.width); y = max(int(pos[1] * H - im.height - 6), 56)
                else:
                    x = y = -3000
                cmds.append((k, f"overlay@{tag_} x {x}, overlay@{tag_} y {y}"))
            cmds.append((tk["end"], f"overlay@{tag_} x -3000, overlay@{tag_} y -3000"))
    cmdf = os.path.join(work, "cmds.txt")
    cmds.sort(key=lambda c: c[0])            # time only: a hide stays after a move at the same moment
    open(cmdf, "w").write("\n".join(f"{t:.2f} {c};" for t, c in cmds))

    se = s.get("start_epoch", 0)
    clock = (f"drawtext=fontfile='{FFONT}':text='{s.get('date_text', '')}  %{{pts\\:localtime\\:{se}\\:%H\\\\\\:%M\\\\\\:%S}}'"
             f":x={xc}:y={STRIP_Y}:fontsize=28:fontcolor=white")

    # ---- pass A: labels on the full footage ----
    crop = f"crop=iw*{area['w']}:ih*{area['h']}:iw*{area['x']}:ih*{area['y']},scale={W}:{H},fps=30"
    inputs = ["-i", src]
    for p, _, _ in layers:
        inputs += ["-i", p]
    cbase = len(layers) + 1
    for p in cin:
        inputs += ["-loop", "1", "-t", str(src_len), "-i", p]
    cmdpath = cmdf.replace("\\", "/").replace(":", "\\:")
    g = f"[0:v]{crop}" + (f",sendcmd=f='{cmdpath}'" if cmds else "") + "[c0];"
    for j in range(len(cin)):
        g += f"[{cbase + j}:v]fps=30[ci{j}];[c{j}][ci{j}]overlay@t{j}=x=-3000:y=-3000:eof_action=pass[c{j+1}];"
    g += f"[c{len(cin)}]null[b0];"
    for i, (_, a, b) in enumerate(layers, 1):
        g += f"[b{i-1}][{i}:v]overlay=0:0:enable='between(t,{a},{b})'[b{i}];"
    g += f"[b{len(layers)}]{clock}[final]"
    stageA = os.path.join(work, "stageA.mp4")
    run([FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex", g, "-map", "[final]", "-t", str(src_len), "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", stageA])

    # ---- waybill zoom (live frame) then freeze on the sharpest frame ----
    wb = s.get("waybill")
    zdir = os.path.join(work, "zoom"); os.makedirs(zdir, exist_ok=True); nz = 0
    if wb:
        zf = os.path.join(work, "zf.png"); sf = os.path.join(work, "sf.png")
        run([FFMPEG, "-v", "error", "-y", "-ss", str(wb["zoom_frame_t"]), "-i", src, "-frames:v", "1", zf])
        run([FFMPEG, "-v", "error", "-y", "-ss", str(wb.get("sharp_t", wb["zoom_frame_t"])), "-i", src, "-frames:v", "1", sf])
        big = Image.open(zf).convert("RGB"); sharp = Image.open(sf).convert("RGB")
        BW, BH = big.size
        A = (area["x"] * BW, area["y"] * BH, area["w"] * BW, area["h"] * BH)
        cx, cy = wb["center"][0] * BW, wb["center"][1] * BH
        zw = wb.get("width", 0.146) * BW
        Bx = (cx - zw / 2, cy - zw / W * H / 2, zw, zw / W * H)
        for i in range(int(ZOOM_S * 30)):
            p = i / (ZOOM_S * 30 - 1); e = p * p * (3 - 2 * p)
            x, y, w, h = [a + (b - a) * e for a, b in zip(A, Bx)]
            fr = big.crop((int(x), int(y), int(x + w), int(y + h))).resize((W, H), Image.LANCZOS)
            dd = ImageDraw.Draw(fr)
            label(dd, "ZOOMING IN ON THE WAYBILL", ((W - tw(dd, "ZOOMING IN ON THE WAYBILL", BOLD)) // 2, 62), YEL)
            fr.save(os.path.join(zdir, f"z{nz:04d}.png")); nz += 1
        x, y, w, h = Bx
        fz = sharp.crop((int(x), int(y), int(x + w), int(y + h)))
        if wb.get("rotate"):
            fz = fz.rotate(wb["rotate"], expand=True)
        fz = fz.resize((W, H), Image.LANCZOS)
        fz = ImageOps.autocontrast(fz, cutoff=1).filter(ImageFilter.UnsharpMask(radius=3, percent=160, threshold=2))
        clean_label = fz.copy()
        dd = ImageDraw.Draw(fz)
        t = "WAYBILL" + ("  (turned upright to read)" if wb.get("rotate") else "")
        label(dd, t, ((W - tw(dd, t, BOLD)) // 2, 62), YEL)
        t = f"ORDER ID  {order}"
        label(dd, t, ((W - tw(dd, t, HUGE)) // 2, H - 120), DRK, WHT, HUGE)
        for i in range(int(FREEZE_S * 30)):
            fz.save(os.path.join(zdir, f"z{nz:04d}.png")); nz += 1

    # ---- summary card ----
    card = Image.new("RGB", (W, H), (24, 24, 30)); cd = ImageDraw.Draw(card)
    if wb:
        lab = clean_label.resize((620, 371), Image.LANCZOS)
        card.paste(lab, (40, 200)); cd.rectangle((36, 196, 664, 575), outline=YEL, width=6)
        cd.text((40, 150), "WAYBILL CLOSE-UP", font=MID, fill=YEL)
    cd.text((700, 120), "ORDER ID", font=BOLD, fill=(200, 200, 210))
    size = 78  # the order ID on one line, as big as fits (it used to break in two and read as two numbers)
    while size > 30 and cd.textlength(order, font=F(size)) > W - 700 - 30:
        size -= 2
    cd.text((700, 170), order, font=F(size), fill=WHT)
    for i, ln in enumerate(s.get("card_lines", [])):
        cd.text((700, 380 + i * 62), ln, font=MID, fill=(110, 220, 130) if i >= 2 else WHT)
    card_png = os.path.join(work, "card.png"); card.save(card_png)

    # ---- pass B: speed, zoom, card ----
    ffx = B.layer(lambda d: label(d, "FAST FORWARD 4x", (26, 12), (245, 192, 68)))
    inB = ["-i", stageA, "-i", ffx, "-loop", "1", "-t", str(CARD_S), "-i", card_png]
    if nz:
        inB += ["-framerate", "30", "-i", os.path.join(zdir, "z%04d.png")]
    g = f"[0:v]split={len(segs)}" + "".join(f"[s{i}]" for i in range(len(segs))) + ";"
    fast = [i for i, (_, _, sp) in enumerate(segs) if sp > 1]
    if fast:
        g += f"[1:v]split={len(fast)}" + "".join(f"[f{i}]" for i in fast) + ";"
    for i, (a, b, sp) in enumerate(segs):
        c = f"[s{i}]trim={a}:{b},setpts=(PTS-STARTPTS)/{sp}"
        g += (c + f"[t{i}];[t{i}][f{i}]overlay=0:0[v{i}];") if sp > 1 else (c + f"[v{i}];")
    g += "[2:v]fps=30,format=yuv420p,setsar=1[card];"
    tail = "[card]"
    if nz:
        g += "[3:v]fps=30,format=yuv420p,setsar=1[zoom];"; tail = "[zoom][card]"
    g += "".join(f"[v{i}]" for i in range(len(segs))) + f"{tail}concat=n={len(segs) + tail.count('[')}:v=1:a=0[out]"
    full = os.path.join(work, "full.mp4")
    run([FFMPEG, "-v", "error", "-y", *inB, "-filter_complex", g, "-map", "[out]", "-an", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", full])

    total = sum((b - a) / sp for a, b, sp in segs) + (ZOOM_S + FREEZE_S if nz else 0) + CARD_S
    parts = math.ceil(total / MAX_PART); plen = total / parts
    made = []
    for pn in range(1, parts + 1):
        def hf(d, pn=pn):
            t = f"VIDEO {pn} / {parts}"
            d.rounded_rectangle((xv - 6, STRIP_Y - 4, W - 18, H - 14), 6, fill=(0, 0, 0, 255))
            d.text((xv, STRIP_Y), t, font=INF, fill=WHT)

        def out_to_src(t):  # time in the sped-up video -> time in the recording
            acc = 0.0
            for a, b_, sp in segs:
                ln = (b_ - a) / sp
                if t <= acc + ln:
                    return a + (t - acc) * sp
                acc += ln
            return segs[-1][1]

        cont_at = None
        if pn < parts:  # the quietest place for the last 3 s of this part, away from hands and parcel
            t = f"CONTINUES IN VIDEO {pn + 1} / {parts}  >>"
            wct = tw(probe, t, ENDF)
            s0, s1 = out_to_src(pn * plen - 3), out_to_src(pn * plen)
            cands = [((W - wct) // 2, 230)] + [(x, y) for y in (90, 230, 380, H - 200) for x in (26, (W - wct) // 2, W - wct - 26)]
            def rc(p_):
                l, t_, r_, b_ = probe.textbbox(p_, t, font=ENDF)
                return (l - 12, t_ - 8, r_ + 12, b_ + 8)
            cont_at = min(cands, key=lambda p_: busy(rc(p_), s0, s1))

        def ef(d, pn=pn, cont_at=cont_at):
            if pn < parts:
                t = f"CONTINUES IN VIDEO {pn + 1} / {parts}  >>"
                label(d, t, cont_at, DRK, WHT, ENDF)
        hdr, end = B.layer(hf), B.layer(ef)
        out = os.path.join(out_dir, f"{base}_video{pn}of{parts}.mp4")
        gg = f"[0:v][1:v]overlay=0:0[a];[a][2:v]overlay=0:0:enable='gte(t,{plen - 3:.2f})'[o]"
        run([FFMPEG, "-v", "error", "-y", "-ss", f"{(pn - 1) * plen:.2f}", "-t", f"{plen:.2f}", "-i", full, "-i", hdr, "-i", end,
             "-filter_complex", gg, "-map", "[o]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "3500k",
             "-maxrate", "3500k", "-bufsize", "7000k", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
        made.append({"file": out, "mb": round(os.path.getsize(out) / 1e6, 1), "seconds": round(plen, 1)})
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"ok": True, "videos": made}))
    return made


if __name__ == "__main__":
    render(sys.argv[1])
