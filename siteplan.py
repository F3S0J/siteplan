"""siteplan engine: JSON site-plan description -> scaled DXF + PDF (+ PNG).

Core conventions
----------------
* Modelspace is drawn in METRES (1 DXF unit = 1 m). Nothing is pre-scaled.
* Every annotation size is expressed in PAPER MILLIMETRES via ``mm()`` so text,
  arrows and hatch spacing stay constant no matter which plot scale is chosen.
* The plot scale is either given or auto-picked: the largest standard scale whose
  content still fits the drawing area of the sheet.
* The sheet frame and title block are stamped onto the PDF in true paper mm, so
  the DXF stays clean site geometry (what a CAD user actually wants).
"""
from __future__ import annotations

import json
import math
import pathlib
from typing import Any

import ezdxf
from ezdxf.enums import TextEntityAlignment
import pymupdf
from ezdxf import bbox, units
from ezdxf.addons.drawing import Frontend, RenderContext, layout
from ezdxf.addons.drawing import pymupdf as pymupdf_backend
from ezdxf.addons.drawing.config import (
    BackgroundPolicy,
    Configuration,
    LineweightPolicy,
)
from ezdxf.math import Vec2, area as polygon_area, offset_vertices_2d
from ezdxf.tools.pattern import load as load_patterns

# Hatch pattern definitions are NOT stored in the DXF file — set_pattern_fill()
# renders nothing unless the definition is passed explicitly.
PATTERNS = load_patterns()

# ----------------------------------------------------------------- sheet setup
SHEETS = {  # portrait dimensions (w, h) in mm
    "A4": (210, 297),
    "A3": (297, 420),
    "A2": (420, 594),
    "A1": (594, 841),
    "A0": (841, 1189),
}
STANDARD_SCALES = [50, 100, 200, 250, 500, 1000, 2000, 2500, 5000]

FRAME = 10.0        # sheet frame inset from the paper edge
BIND = 20.0         # wider left margin for filing
TB_W, TB_H = 180.0, 40.0   # title block, bottom-right inside the frame
PAD = 5.0           # gap between frame/title block and the drawing content

# ------------------------------------------------------------------- layers
# name -> (aci colour, lineweight 1/100 mm, linetype)
LAYERS = {
    "GRENZE":    (1, 50, "CONTINUOUS"),   # parcel boundary, red
    "GEBAEUDE":  (7, 35, "CONTINUOUS"),   # buildings
    "STRASSE":   (8, 18, "CONTINUOUS"),   # roads
    "BAUFLUCHT": (3, 18, "DASHED2"),      # setback / building lines
    "FLAECHE":   (8, 13, "CONTINUOUS"),   # parking, green space, yards
    "BEMASSUNG": (4, 13, "CONTINUOUS"),   # dimensions
    "TEXT":      (7, 13, "CONTINUOUS"),
    "SCHRAFFUR": (9,  9, "CONTINUOUS"),
}

# area "kind" -> (hatch pattern, paper scale mm/pattern-unit, aci colour).
# None = outline only. See hatch() for what the scale means.
AREA_STYLES = {
    "parking": (None, 0, 8),
    "green":   ("GRASS", 0.25, 3),
    "yard":    ("AR-SAND", 0.35, 9),
    "gravel":  ("GRAVEL", 0.8, 9),
    "water":   ("ANSI37", 0.5, 5),
    "wood":    ("ANSI31", 0.35, 34),   # timber structures (huts), brown
    "concrete": ("AR-CONC", 0.03, 9),  # paved/concrete work areas
    # AR-CONC repeats every ~200 pattern units -> tiny paper scale needed
    "plain":   (None, 0, 8),
}


def _pts(seq) -> list[tuple[float, float]]:
    return [(float(p[0]), float(p[1])) for p in seq]


def _rect(spec) -> list[tuple[float, float]]:
    """[x, y, w, h] (+ optional rotation in degrees about the corner) -> corners."""
    x, y, w, h = (float(v) for v in spec[:4])
    rot = math.radians(float(spec[4])) if len(spec) > 4 else 0.0
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    c, s = math.cos(rot), math.sin(rot)
    return [(x + cx * c - cy * s, y + cx * s + cy * c) for cx, cy in corners]


def outline(obj: dict) -> list[tuple[float, float]]:
    """Accept either an explicit 'points' polygon or a 'rect' shorthand."""
    if "points" in obj:
        return _pts(obj["points"])
    if "rect" in obj:
        return _rect(obj["rect"])
    raise ValueError(f"object needs 'points' or 'rect': {obj.get('name', obj)}")


def centroid(pts) -> tuple[float, float]:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def fmt_area(a: float) -> str:
    """German thousands separator: 4283 -> '4.283'."""
    return f"{a:,.0f}".replace(",", ".")


# ------------------------------------------------------------------ building
def build_doc(plan: dict, scale: int):
    """Create the DXF document for `plan` at 1:`scale`. Returns (doc, stats)."""

    def mm(paper_mm: float) -> float:
        """paper millimetres -> model metres at this plot scale"""
        return paper_mm * scale / 1000.0

    doc = ezdxf.new("R2018", setup=True)
    doc.units = units.M
    doc.header["$INSUNITS"] = 6          # metres
    doc.header["$LTSCALE"] = mm(3)
    msp = doc.modelspace()

    for name, (color, lw, lt) in LAYERS.items():
        doc.layers.add(name, color=color, lineweight=lw, linetype=lt)

    dim = doc.dimstyles.duplicate_entry("EZDXF", "SITE")
    dim.dxf.dimtxt = mm(2.5)
    dim.dxf.dimasz = mm(2.0)
    dim.dxf.dimexe = mm(1.0)
    dim.dxf.dimexo = mm(1.0)
    dim.dxf.dimgap = mm(0.8)
    dim.dxf.dimdec = 2
    dim.dxf.dimlunit = 2
    dim.dxf.dimlfac = 1.0            # DXF units are metres -> print metres
    dim.dxf.dimpost = "<> m"
    dim.dxf.dimclrd = dim.dxf.dimclre = dim.dxf.dimclrt = 4

    def text(s, pos, height_mm=2.5, layer="TEXT", rotation=0.0):
        """Single-line labels go in as TEXT, multi-line ones as MTEXT.

        MTEXT without a column width renders as one line but is MEASURED by
        ezdxf as if wrapped at a small default width — a 49-character heading
        reports 24 m of height instead of 3 m, which throws off scale
        selection and the overflow check. TEXT is measured from the real
        glyph metrics, so the sheet fit matches what is printed."""
        h = mm(height_mm)
        if "\\P" in s or "\n" in s:
            t = msp.add_mtext(s, dxfattribs={
                "layer": layer, "char_height": h,
                "style": "Standard", "rotation": rotation,
            })
            # column wide enough that it only breaks at the explicit \P
            lines = s.replace("\\P", "\n").split("\n")
            longest = max((len(ln) for ln in lines), default=1)
            t.dxf.width = max(longest, 1) * h * 0.9
            t.set_location(pos, attachment_point=5)   # MIDDLE_CENTER
            return t
        t = msp.add_text(s, dxfattribs={
            "layer": layer, "height": h, "style": "Standard",
            "rotation": rotation,
        })
        t.set_placement(pos, align=TextEntityAlignment.MIDDLE_CENTER)
        return t

    def hatch(points, pattern, paper_mm, color=9, angle=0.0):
        """paper_mm = size of one pattern unit on PAPER, in mm.
        ANSI31 has a 3.175-unit line spacing, so paper_mm=0.5 gives ~1.6 mm
        between hatch lines on the printed sheet at any plot scale."""
        definition = PATTERNS.get(pattern.upper())
        if definition is None:
            raise ValueError(f"unknown hatch pattern {pattern!r}")
        h = msp.add_hatch(color=color, dxfattribs={"layer": "SCHRAFFUR"})
        h.paths.add_polyline_path(points, is_closed=True)
        h.set_pattern_fill(pattern, definition=definition, scale=mm(paper_mm),
                           angle=angle, color=color)

    def solid(points, layer="TEXT"):
        h = msp.add_hatch(color=7, dxfattribs={"layer": layer})
        h.paths.add_polyline_path(points, is_closed=True)

    stats: dict[str, Any] = {"parcels": [], "buildings": [], "gfa": 0.0}

    # ---------------------------------------------------------------- streets
    for st in plan.get("streets", []):
        centre = _pts(st["centerline"])
        width = float(st.get("width", 6))
        left = [tuple(p) for p in offset_vertices_2d(centre, offset=width / 2)]
        right = [tuple(p) for p in offset_vertices_2d(centre, offset=-width / 2)]
        msp.add_lwpolyline(left + list(reversed(right)), close=True,
                           dxfattribs={"layer": "STRASSE"})
        if st.get("centerline_dashed", True):
            msp.add_lwpolyline(centre, dxfattribs={"layer": "BAUFLUCHT"})
        if st.get("name"):
            a, b = Vec2(centre[0]), Vec2(centre[-1])
            ang = math.degrees((b - a).angle)
            if ang > 90 or ang < -90:
                ang += 180
            text(st["name"], a.lerp(b, 0.5) + Vec2(0, -mm(3.5)).rotate_deg(ang),
                 3.0, layer="STRASSE", rotation=ang)

    # ---------------------------------------------------------------- areas
    for ar in plan.get("areas", []):
        pts = outline(ar)
        kind = ar.get("kind", "plain")
        pattern, spacing, color = AREA_STYLES.get(kind, AREA_STYLES["plain"])
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "FLAECHE"})
        if pattern:
            hatch(pts, pattern, spacing, color=color)
        if kind == "parking" and ar.get("stalls"):
            _draw_stalls(msp, pts, int(ar["stalls"]))
        if ar.get("name"):
            cx, cy = centroid(pts)
            label = ar["name"]
            if ar.get("stalls"):
                label = f"{ar['stalls']} {ar.get('stall_word', 'Stellplätze')}"
            ymin = min(p[1] for p in pts)
            text(label, (cx, ymin - mm(3.0)), 2.2, layer="FLAECHE")

    # ---------------------------------------------------------------- parcels
    for pc in plan.get("parcels", []):
        pts = outline(pc)
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "GRENZE"})
        a = abs(polygon_area(pts))
        stats["parcels"].append({"nr": pc.get("nr", "?"), "area": a})
        if pc.get("label", True):
            head = pc.get("title") or (f"Grst. Nr. {pc['nr']}" if pc.get("nr") else "")
            body = f"{head}\\P{fmt_area(a)} m²" if pc.get("show_area", True) else head
            anchor = pc.get("label_at")
            if anchor is None:
                cx = centroid(pts)[0]
                ymax = max(p[1] for p in pts)
                anchor = (cx, ymax - mm(9))
            text(body, tuple(anchor), float(pc.get("label_size", 4.0)))

    # ---------------------------------------------------------- setback lines
    for ln in plan.get("lines", []):
        pts = _pts(ln["points"])
        msp.add_lwpolyline(pts, dxfattribs={"layer": "BAUFLUCHT"})
        if ln.get("label"):
            end = pts[-1]
            text(ln["label"], (end[0] - mm(12), end[1] + mm(2.0)), 2.0,
                 layer="BAUFLUCHT")

    # -------------------------------------------------------------- buildings
    for bl in plan.get("buildings", []):
        pts = outline(bl)
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "GEBAEUDE"})
        hatch(pts, bl.get("hatch", "ANSI31"), float(bl.get("hatch_scale", 0.5)))
        a = abs(polygon_area(pts))
        stats["buildings"].append({"name": bl.get("name", "?"), "area": a})
        stats["gfa"] += a
        parts = [p for p in (bl.get("name"), bl.get("use")) if p]
        if bl.get("show_area", True):
            parts.append(f"{fmt_area(a)} m²")
        if parts:
            text("\\P".join(parts), centroid(pts),
                 float(bl.get("label_size", 2.8)))

    # ----------------------------------------------------- free-standing labels
    for lb in plan.get("labels", []):
        text(lb["text"], tuple(lb["at"]), float(lb.get("size", 2.0)),
             layer=lb.get("layer", "TEXT"), rotation=float(lb.get("rotation", 0)))

    # ------------------------------------------------------------- dimensions
    dims = plan.get("dimensions", "auto")
    if dims == "auto":
        dims = _auto_dimensions(plan, mm)
    for d in dims or []:
        p1, p2 = tuple(d["from"]), tuple(d["to"])
        ang = float(d.get("angle", 0))
        off = float(d.get("offset", 8))
        base = _dim_base(p1, p2, off, ang, mm)
        msp.add_linear_dim(base=base, p1=p1, p2=p2, angle=ang, dimstyle="SITE",
                           dxfattribs={"layer": "BEMASSUNG"}).render()

    # ------------------------------------------------------------ north arrow
    if plan.get("north") is not False:
        nx, ny = _north_position(plan, mm)
        solid([(nx, ny + mm(8)), (nx - mm(3), ny - mm(5)), (nx, ny - mm(2))])
        msp.add_lwpolyline(
            [(nx, ny + mm(8)), (nx + mm(3), ny - mm(5)), (nx, ny - mm(2))],
            close=True, dxfattribs={"layer": "TEXT"})
        text("N", (nx, ny + mm(12)), 4.0)

    # -------------------------------------------------------------- scale bar
    if plan.get("scalebar", True):
        _draw_scalebar(msp, plan, scale, mm, text, solid)

    return doc, stats


def _draw_stalls(msp, pts, n):
    """Divide a parking rectangle into n bays along its longer axis."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if (x1 - x0) >= (y1 - y0):
        step = (x1 - x0) / n
        for i in range(1, n):
            msp.add_line((x0 + i * step, y0), (x0 + i * step, y1),
                         dxfattribs={"layer": "FLAECHE"})
    else:
        step = (y1 - y0) / n
        for i in range(1, n):
            msp.add_line((x0, y0 + i * step), (x1, y0 + i * step),
                         dxfattribs={"layer": "FLAECHE"})


def _dim_base(p1, p2, offset_m, angle, mm):
    """Place the dimension line `offset_m` metres outside the measured segment."""
    a, b = Vec2(p1), Vec2(p2)
    mid = a.lerp(b, 0.5)
    if abs(angle - 90) < 1e-6:
        return (mid.x + offset_m, mid.y)
    return (mid.x, mid.y + offset_m)


def _bounds(plan):
    xs, ys = [], []
    for key in ("parcels", "buildings", "areas"):
        for o in plan.get(key, []):
            for x, y in outline(o):
                xs.append(x)
                ys.append(y)
    if not xs:
        raise ValueError("plan contains no parcels/buildings/areas to draw")
    return min(xs), min(ys), max(xs), max(ys)


def _auto_dimensions(plan, mm):
    """Dimension the overall width and depth of the first parcel.

    Offsets are PAPER millimetres, so annotation takes the same margin on the
    sheet whether the site is 50 m or 500 m across."""
    parcels = plan.get("parcels", [])
    if not parcels:
        return []
    pts = outline(parcels[0])
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return [
        {"from": (x0, y0), "to": (x1, y0), "angle": 0, "offset": -mm(11)},
        {"from": (x0, y0), "to": (x0, y1), "angle": 90, "offset": -mm(11)},
    ]


def _north_position(plan, mm):
    if isinstance(plan.get("north"), (list, tuple)):
        return tuple(plan["north"])
    x0, y0, x1, y1 = _bounds(plan)
    return (x1 + mm(14), y1 - mm(10))


def _draw_scalebar(msp, plan, scale, mm, text, solid):
    x0, y0, x1, y1 = _bounds(plan)
    span = x1 - x0
    # a "nice" segment length: ~15 mm on paper, snapped to 1/2/5 x 10^n
    raw = mm(15)
    exp = 10 ** math.floor(math.log10(max(raw, 1e-9)))
    seg = min((1, 2, 5, 10), key=lambda m: abs(m * exp - raw)) * exp
    bx, by = x0, y0 - mm(20)      # constant paper offset, not a % of the site
    h = mm(1.5)
    for i in range(4):
        box = [(bx + i * seg, by), (bx + (i + 1) * seg, by),
               (bx + (i + 1) * seg, by + h), (bx + i * seg, by + h)]
        if i % 2 == 0:
            solid(box)
        else:
            msp.add_lwpolyline(box, close=True, dxfattribs={"layer": "TEXT"})
        text(f"{i * seg:g}", (bx + i * seg, by - mm(2.5)), 2.0)
    text(f"{4 * seg:g} m", (bx + 4 * seg, by - mm(2.5)), 2.0)
    text(f"Maßstab 1:{scale}", (bx + 2 * seg, by + mm(4)), 2.5)


# ------------------------------------------------------------ scale selection
def sheet_size(plan) -> tuple[float, float]:
    sheet = plan.get("sheet", "A3")
    if isinstance(sheet, (list, tuple)):
        return float(sheet[0]), float(sheet[1])
    w, h = SHEETS[str(sheet).upper()]
    if plan.get("orientation", "landscape").lower() == "landscape":
        w, h = h, w
    return float(w), float(h)


def margins(plan) -> tuple[float, float, float]:
    """(frame, bind, pad) in mm. plan["margins"] overrides the defaults —
    a number sets all three edges, a dict sets them individually. Use it for
    a sheet that is meant to be filled edge to edge; ~5 mm is the practical
    floor because most printers cannot print closer to the paper edge."""
    m = plan.get("margins")
    if m is None:
        return FRAME, BIND, PAD
    if isinstance(m, (int, float)):
        return float(m), float(m), 0.0
    return (float(m.get("frame", FRAME)), float(m.get("bind", BIND)),
            float(m.get("pad", PAD)))


def drawing_area(plan) -> tuple[float, float]:
    """Usable drawing area in mm, after frame, binding edge and title block."""
    w, h = sheet_size(plan)
    frame, bind, pad = margins(plan)
    tb = TB_H + PAD if plan.get("titleblock", True) else 0.0
    return (w - (bind + pad) - (frame + pad),
            h - (frame + pad) * 2 - tb)


def choose_scale(plan, verbose=True):
    """Largest standard scale whose content fits the drawing area.

    With orientation "auto" both sheet orientations are tried and the one
    allowing the larger scale wins; the winner is written back into `plan`.
    """
    fixed = plan.get("scale")
    if fixed:
        doc, stats = build_doc(plan, int(fixed))
        ext = bbox.extents(doc.modelspace(), fast=False)
        avail_w, avail_h = drawing_area(plan)
        w = ext.size.x * 1000 / int(fixed)
        h = ext.size.y * 1000 / int(fixed)
        if verbose and (w > avail_w or h > avail_h):
            print(f"  ! 1:{fixed} needs {w:.0f} x {h:.0f} mm but only "
                  f"{avail_w:.0f} x {avail_h:.0f} mm is available — content will overflow")
        return int(fixed), doc, stats, ext

    requested = str(plan.get("orientation", "landscape")).lower()
    orientations = (["landscape", "portrait"] if requested == "auto"
                    else [requested])

    best = None
    for orient in orientations:
        plan["orientation"] = orient
        avail_w, avail_h = drawing_area(plan)
        for sc in STANDARD_SCALES:
            doc, stats = build_doc(plan, sc)
            ext = bbox.extents(doc.modelspace(), fast=False)
            w = ext.size.x * 1000 / sc
            h = ext.size.y * 1000 / sc
            fits = w <= avail_w and h <= avail_h
            if verbose:
                tag = f"[{orient[:4]}] " if len(orientations) > 1 else ""
                print(f"  {tag}1:{sc:<5} -> {w:7.1f} x {h:7.1f} mm   "
                      f"{'FITS' if fits else 'too big'}")
            if fits:
                if best is None or sc < best[0]:
                    best = (sc, orient, doc, stats, ext)
                break

    if best is None:
        raise ValueError("content does not fit even at 1:5000 — use a bigger sheet")
    sc, orient, doc, stats, ext = best
    plan["orientation"] = orient
    if verbose and len(orientations) > 1:
        print(f"  -> {orient} at 1:{sc}")
    return sc, doc, stats, ext


# --------------------------------------------------------------- PDF output
def render_pdf(doc, plan, scale, out_pdf: pathlib.Path):
    sheet_w, sheet_h = sheet_size(plan)
    frame, bind, pad = margins(plan)
    tb = TB_H + PAD if plan.get("titleblock", True) else 0.0

    cfg = Configuration(
        lineweight_policy=LineweightPolicy.ABSOLUTE,
        background_policy=BackgroundPolicy.WHITE,
    )
    ctx = RenderContext(doc)
    ctx.set_current_layout(doc.modelspace())
    # white paper => ACI colour 7 resolves to BLACK instead of white
    ctx.current_layout_properties.set_colors(bg="#FFFFFF")

    backend = pymupdf_backend.PyMuPdfBackend()
    Frontend(ctx, backend, config=cfg).draw_layout(doc.modelspace())

    page = layout.Page(
        sheet_w, sheet_h, layout.Units.mm,
        layout.Margins(top=frame + pad, right=frame + pad,
                       bottom=frame + pad + tb, left=bind + pad),
    )
    settings = layout.Settings(fit_page=False, scale=1000 / scale)
    out_pdf.write_bytes(backend.get_pdf_bytes(page, settings=settings))
    return out_pdf


def stamp_sheet(pdf_path: pathlib.Path, plan: dict, scale: int, stats: dict):
    """Draw the sheet frame and title block in true paper mm onto the PDF."""
    if not plan.get("titleblock", True):
        return
    sheet_w, sheet_h = sheet_size(plan)
    FRAME_, BIND_, _pad = margins(plan)
    doc = pymupdf.open(pdf_path)
    page = doc[0]
    k = page.rect.width / sheet_w          # pt per mm, from the real page

    def P(x_mm, y_mm):                     # mm from bottom-left -> PDF point
        return pymupdf.Point(x_mm * k, (sheet_h - y_mm) * k)

    def line(x1, y1, x2, y2, w=0.25):
        # pymupdf widths are in points; k converts mm -> pt for this page
        page.draw_line(P(x1, y1), P(x2, y2), color=(0, 0, 0), width=w * k)

    def label(x_mm, y_mm, s, size=2.5, bold=False):
        page.insert_text(P(x_mm, y_mm), s, fontsize=size * k,
                         fontname="hebo" if bold else "helv", color=(0, 0, 0))

    # sheet frame
    frame = pymupdf.Rect(P(BIND_, FRAME_).x, P(BIND_, sheet_h - FRAME_).y,
                         P(sheet_w - FRAME_, FRAME_).x, P(BIND_, FRAME_).y)
    page.draw_rect(frame, color=(0, 0, 0), width=0.5 * k)

    # title block, bottom-right inside the frame
    tb_x0, tb_y0 = sheet_w - FRAME_ - TB_W, FRAME_
    tb_x1, tb_y1 = sheet_w - FRAME_, FRAME_ + TB_H
    page.draw_rect(pymupdf.Rect(P(tb_x0, tb_y1).x, P(tb_x0, tb_y1).y,
                               P(tb_x1, tb_y0).x, P(tb_x1, tb_y0).y),
                   color=(0, 0, 0), width=0.5 * k)

    meta = plan.get("meta", {})
    total_area = sum(p["area"] for p in stats["parcels"])

    # upper band: project + plan title
    line(tb_x0, tb_y0 + 24, tb_x1, tb_y0 + 24, 0.5)
    label(tb_x0 + 3, tb_y0 + 33.5, meta.get("project", plan.get("title", "")), 3.2, bold=True)
    label(tb_x0 + 3, tb_y0 + 28.5, meta.get("subtitle", "Lageplan"), 2.4)

    # lower band: four data cells
    cells = [
        # meta["scale_text"] lets a pure text sheet (notes, signature block)
        # say "ohne Maßstab" instead of quoting a scale nothing was drawn at
        ("Maßstab", meta.get("scale_text") or f"1:{scale}"),
        ("Fläche", f"{fmt_area(total_area)} m²" if total_area else "—"),
        ("Datum", meta.get("date", "")),
        ("Plan-Nr.", meta.get("plan_no", "")),
    ]
    cw = TB_W / len(cells)
    for i, (head, val) in enumerate(cells):
        cx = tb_x0 + i * cw
        if i:
            line(cx, tb_y0, cx, tb_y0 + 24, 0.3)
        label(cx + 3, tb_y0 + 18, head, 2.0)
        label(cx + 3, tb_y0 + 11, str(val), 3.0, bold=True)

    if meta.get("drawn_by"):
        label(tb_x0 + 3, tb_y0 + 4, f"Bearbeiter: {meta['drawn_by']}", 2.0)
    if meta.get("client"):
        label(tb_x0 + cw * 2 + 3, tb_y0 + 4, f"Bauherr: {meta['client']}", 2.0)

    doc.saveIncr()


def to_png(pdf_path: pathlib.Path, png_path: pathlib.Path, dpi=150):
    pymupdf.open(pdf_path)[0].get_pixmap(dpi=dpi).save(png_path)


# ------------------------------------------------------------------- driver
def generate(plan_path, outdir=None, png=True, verbose=True):
    plan_path = pathlib.Path(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    outdir = pathlib.Path(outdir) if outdir else plan_path.parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = plan.get("output", plan_path.stem)

    scale, doc, stats, ext = choose_scale(plan, verbose=verbose)

    dxf = outdir / f"{stem}.dxf"
    pdf = outdir / f"{stem}.pdf"
    doc.saveas(dxf)
    render_pdf(doc, plan, scale, pdf)
    stamp_sheet(pdf, plan, scale, stats)
    if png:
        to_png(pdf, outdir / f"{stem}.png")

    if verbose:
        sw, sh = sheet_size(plan)
        print(f"\nscale 1:{scale}   sheet {sw:g} x {sh:g} mm")
        for p in stats["parcels"]:
            print(f"  parcel {p['nr']:<10} {fmt_area(p['area']):>10} m²")
        named = [b for b in stats["buildings"] if b["name"] != "?"]
        for b in named:
            print(f"  building {b['name']:<18} {fmt_area(b['area']):>8} m²")
        anon = len(stats["buildings"]) - len(named)
        if anon:
            area = sum(b["area"] for b in stats["buildings"] if b["name"] == "?")
            print(f"  {anon} unnamed building footprints, {fmt_area(area)} m² total")
        if stats["parcels"] and stats["gfa"]:
            total = sum(p["area"] for p in stats["parcels"])
            print(f"  built-up ratio (BGF/Grundfläche): {stats['gfa'] / total:.1%}")
        print(f"\nwrote {dxf.name}, {pdf.name}" + (f", {stem}.png" if png else ""))

    return {"scale": scale, "stats": stats, "dxf": dxf, "pdf": pdf}
