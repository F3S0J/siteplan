# Agent instructions — siteplan

Operating manual for an AI agent (or a human in a hurry) that uses this repo to
produce scaled site plans. Read `README.md` for the overview; this file holds the
workflow, the schema, and every trap that has already cost a wrong drawing.

Use this tool when someone gives property / land / building dimensions and wants
a drawing, plan, CAD file, DXF or scaled graphic of a site (Lageplan,
Teilungsplan, Grundstücksplan, site plan, plot plan); names an Austrian plot by
Katastralgemeinde (KG) and Grundstücksnummer; or asks what fits on a plot, how
much of it to buy, or how many units it yields.

Geometry goes in as metres in a **JSON plan file**; the engine picks the plot
scale, lays out the sheet, and emits **DXF + PDF + PNG**.

## Files

```
siteplan.py                  # engine: geometry, scale selection, PDF sheet stamping
build.py                     # CLI: plan.json -> DXF + PDF + PNG
verify.py                    # round-trip check (DXF metres + printed mm) — run after edits
checkplan.py                 # re-measure a FINISHED plan: areas, setbacks, containment
kataster.py                  # live Austrian cadastre (BEV): plot number -> real geometry
example/musterdorf.json      # complete worked example (fictional) — copy this to start
example/from_kataster.py     # worked example: KG + plot number -> finished plan
```

## Workflow

1. **Collect the measurements.** Never invent a dimension, a plot number or an
   area — if a length is missing, ask. Areas are *computed from the geometry*, so
   they are always consistent with the drawing.
2. **Write a plan JSON** (schema below). Copy `example/musterdorf.json`.
3. **Build:**
   ```
   python build.py myplan.json -o out/
   ```
4. **Look at the PNG** before delivering. Check: nothing runs off the frame,
   labels don't collide, the chosen scale is sensible, the title block is filled.
5. **Audit the output, not your variables:**
   ```
   python checkplan.py out/myplan.dxf --setback 3.0 --parent parent.geojson
   ```
   Non-negotiable whenever a statutory distance is involved. Every real error
   found so far was invisible from inside the generator: a setback that read
   "2 m" in the code and pinched to 1,15 m against a skewed boundary; a building
   sitting exactly on a plot line; a second sheet that had silently gone stale.
   `checkplan.py` ignores cadastral context buildings (anything wholly outside the
   reference parcel) so only the project's own buildings are measured.
6. **Deliver** the PDF (for printing/authorities) *and* the DXF (for anyone who
   needs to edit it). State the scale and the computed areas.

After changing `siteplan.py`, run `python verify.py example/musterdorf.json` —
it re-reads the DXF and measures the PDF; it must print **PASS**.

If a job produces **more than one sheet** (a site plan plus a detail), derive the
second sheet's dimensions from the first — dump them to a small `dims.json` and
read it. Hard-coding the same numbers twice means the detail keeps the *old*
geometry the moment the layout changes, and nothing warns you.

Variant studies always grow ("now with pools", "now 6 houses", "remove the
garages"): keep **one generator script with a function per variant** sharing a
`base()` for context/roads/title and shared zoning constants. Then a change
request is a small edit + rebuild of that one variant, revisions stay consistent
across variants, and a variant can be reverted without touching the others.

## Plan schema

```jsonc
{
  "sheet": "A3",              // A4 | A3 | A2 | A1 | A0, or [width_mm, height_mm]
  "orientation": "auto",      // "auto" tries both and keeps the larger scale
  "scale": null,              // null = auto-pick; or 500 for a forced 1:500
  "output": "lageplan_100-1", // optional output file stem
  "margins": {"frame": 5, "bind": 5, "pad": 1},   // default 10 / 20 / 5 mm

  "meta": {                   // title block — all fields optional
    "project":  "Gewerbepark Nord — Grst. Nr. 1274/3",
    "subtitle": "Lageplan · Bestand und geplante Bebauung",
    "date":     "14.08.2026",
    "plan_no":  "LP-2026-014",
    "drawn_by": "M. Muster",
    "client":   "Muster Immobilien GmbH",
    "scale_text": "ohne Maßstab"   // overrides "1:N" in the title block
  },

  "parcels": [                // land plots — boundary in RED
    { "nr": "1274/3",
      "points": [[0,0],[80,0],[80,42],[62,55],[0,55]],
      "show_area": true,      // append the computed m² to the label
      "title": "…",           // override the "Grst. Nr. X" heading
      "label_at": [20,48],    // override label position
      "label_size": 4.0 }     // text height in PAPER mm
  ],

  "buildings": [              // hatched footprints, area auto-computed
    { "name": "Halle A", "use": "Lager", "rect": [8, 20, 34, 20] },
    { "name": "Halle B", "points": [[48,22],[72,22],[72,38],[48,38]] },
    { "name": "Schuppen", "rect": [8, 6, 18, 9, 30] }   // 5th value = rotation°
  ],

  "streets": [                // drawn from a centreline + width
    { "name": "Gewerbestraße", "centerline": [[-14,-4],[94,-4]], "width": 8,
      "centerline_dashed": true }
  ],

  "areas": [                  // parking / green / yard / gravel / water / wood / concrete / plain
    { "name": "Parkplatz", "kind": "parking", "rect": [46,4,28,12], "stalls": 11 },
    { "name": "Grünfläche", "kind": "green",  "rect": [28,6,14,9] }
  ],

  "lines": [                  // setback / building lines, dashed green
    { "points": [[0,5],[80,5]], "label": "Baufluchtlinie" }
  ],

  "labels": [                 // free-standing text, e.g. neighbouring plot numbers
    { "text": "158/2", "at": [12.5, 40.0], "size": 1.8, "rotation": 0 }
  ],

  "dimensions": "auto",       // "auto" = overall width + depth of the first parcel
  // or an explicit list:
  // [ { "from":[8,40], "to":[42,40], "angle":0, "offset":6 } ]
  //   angle 0 = horizontal measurement, 90 = vertical; offset in METRES

  "north": [88, 46],          // arrow position; omit = auto (right of content), false = hide
  "scalebar": true,
  "titleblock": true
}
```

`rect` is `[x, y, width, height]` with the origin at the lower-left corner —
shorthand for the equivalent `points` polygon. Use `points` for anything
non-rectangular. All coordinates and lengths are **metres**. Sheet labels and
layer names are German (Austrian drafting convention: GRENZE, GEBAEUDE,
STRASSE, BAUFLUCHT, BEMASSUNG …).

## How scale works — do not re-derive this

- Modelspace is **metres**, 1 DXF unit = 1 m. Nothing is pre-scaled.
- Every annotation size is written as `mm(paper_mm)` = `paper_mm * scale / 1000`,
  so text stays 2.5 mm and hatching ~1.6 mm on paper at *any* plot scale.
- The PDF is emitted with `Settings(fit_page=False, scale=1000/N)` for 1:N. This is
  **verified true scale** — an 80 m edge measures 159.93 mm at 1:500 against a
  160.00 mm target. The 0.05 % is PDF point quantisation of the page itself
  (A3 lands at 419.8 mm, not 420.0), not drawing error.
- Auto-scale walks `STANDARD_SCALES` (1:50 → 1:5000) and takes the first that fits
  the drawing area, i.e. the sheet minus frame, binding edge and title block.
  If nothing fits, it raises — move to a bigger sheet.
- `plan["margins"]` (a number, or `{frame, bind, pad}`) shrinks those edges when a
  sheet should be filled to the paper. **~5 mm is the floor** — most printers
  cannot print closer. `titleblock: false` removes the title block *and the sheet
  frame*, so the only box left is the map border produced by clipping.

## Getting a legible sheet out of a small format

The scale, not the sheet size, is what makes a plan readable: annotation is written
in **paper millimetres**, so halving the scale leaves the text the same size on
paper while the drawing shrinks — a label's footprint *inside the drawing* doubles.
A sheet whose labelling works at 1:500 collides at 1:1000. So when a big plan has to
go onto a small sheet, in this order:

1. **Cut what the audience does not care about.** A roundabout 100 m away, a
   neighbour parcel nobody signs, the far side of a field. Extent is the only free
   variable that does not cost legibility, and it usually buys a whole scale step.
2. **Rotate the drawing 90°** when the content is wide-and-flat and the sheet is
   portrait (or the reverse). A 132 × 98 m corridor does not fit A4 portrait at
   1:500; rotated it does.
3. **Drop the sheet furniture.** Title block and scale bar together cost ~45 mm of
   height; put the scale, the date and the plan number into a small boxed panel
   placed **over an empty part of the drawing** instead of reserving a band for it
   — a reserved band costs a scale step, a panel costs nothing. Then say the scale
   in that panel, because the scale bar is gone.
4. Only then go to a bigger sheet.

### Rotating a plan

The engine draws +y up and has no rotation; rotate the finished plan dict yourself
(`(x, y) -> (y, -x)` for 90° clockwise, which puts north to the right).

- Rotate `points`, label `at`, dimension `from`/`to`, **and `parcels[].label_at`** —
  that last one is easy to miss, and a roughly square parcel centred near the origin
  will keep its title inside the boundary by luck, so the bug does not look like one.
- Add the rotation to each dimension's `angle`.
- **Text: rotate only what belongs to a feature.** A label with rotation 0 is free
  text (parcel numbers, notes) and must stay horizontal; a label with a non-zero
  rotation is aligned to a street, a road band or an axis and has to turn with it,
  then be normalised into (-90°, +90°] so it never comes out upside down.
- The engine's north arrow always points up — set `"north": false` and draw your own
  in the rotated north direction (a `plain` polygon plus an "N" label).
- Labels far outside the new window are clipped away — the street name is the classic
  loss. Re-place them, and **derive the position from geometry** (intersect a line
  with the street parcel and take the centroid) instead of guessing at coordinates.

### Deriving extra sheets from one plan

Do not rebuild a second sheet from the same numbers — *transform the finished plan
dict*: clip it to a window, rotate it, move a handful of labels. Then a geometry
change in the generator propagates to every sheet automatically, and the sheets
cannot drift apart. Useful pieces:

- A clip pass that intersects parcels/buildings/areas/lines with a world box and
  drops labels and dimensions by their anchor point. Clipped parcels leave a
  rectangular border around the map — that reads as a map frame, it is not a defect.
- **Assert that every drop/rename key actually matched a label.** Otherwise an edited
  string silently stops being dropped and reappears on the sheet.
- Filter dimensions by length (short ones are noise on an overview) or by whether
  their midpoint falls inside a given polygon — dropping every dimension inside the
  house is how you get a street plan instead of a building plan.
- A pure text sheet (notes, legend, signature table) is easiest at a forced 1:1000:
  1 m of model is then exactly 1 mm of paper, so the layout is plain millimetre
  arithmetic. Give it `north: false`, `scalebar: false`, `titleblock: false` and
  `meta["scale_text"]`.
- **Measure every figure you print from the drawn geometry** — road area per parcel
  from the corridor polygon, length from the band edges — never retype it. A number
  typed into a note is the one thing that can contradict the drawing.

## Austrian cadastre (`kataster.py`) — real plots, no hand-typed coordinates

For a plot anywhere in Austria you do **not** need measurements from the owner.
Two free BEV services (no key, CC BY 4.0, updated daily) provide the real boundary:

```python
import kataster as K
kg     = K.find_kg("<Katastralgemeinde>")        # -> kg_nr, meridian, Bundesland
parcel = K.find_parcel(kg["kg_nr"], "<Gst-Nr>")  # -> EZ, status, bbox, centre
ctx    = K.fetch_context(*parcel["center"], radius_m=250,
                         meridian=kg["meridian"],
                         cover_bbox=parcel["bbox"])   # <- see the truncation trap
print(K.summarise(ctx, focus_gnr="<Gst-Nr>"))
print(K.validate_parcel(ctx, "<Gst-Nr>"))
plan = K.to_siteplan(ctx, focus_gnr="<Gst-Nr>", meta={...})
```

End to end:

```
python example/from_kataster.py <Katastralgemeinde> <Grundstücksnummer> [radius_m]
```

You get the focus parcel with its true area, every neighbouring parcel with its
plot number, all building footprints and — the point for street planning — the
existing **Straßenverkehrsanlagen** (land-use code 95), **Bahnflächen** (92) and
**Forststraßen** (58): where a road already exists, where one does not, and which
parcels a new access road would have to cross.

### The services

| | |
|---|---|
| Search / locator | `https://kataster.bev.gv.at/at.gv.bev.kataster/api/all/?term=<text>&layers=<A-B-C>` |
| Vector tiles | `https://kataster.bev.gv.at/tiles/kataster/{z}/{x}/{y}.pbf` |

- Search layer codes are joined with **hyphens**, not commas (commas → HTTP 500).
- Search returns a **bounding box only** — it is a locator, not geometry.
- Real geometry comes from the tiles. **Zoom 16 only**: extent 65536 → ~6 mm
  ground quantisation. z15 has extent 512 (1.6 m) and is visibly generalised;
  z17 does not exist (404).
- Tiles are EPSG:3857 → reproject to the KG's Gauss-Krüger zone via its
  `meridian` field (28→EPSG:31254, 31→31255, 34→31256).
- Attribution is required: *"Datenquelle: BEV — data.bev.gv.at (CC BY 4.0)"*.

### Traps that produce silently wrong areas

- **Truncation.** Tiles snap to ~410 m at Austrian latitudes. A parcel longer
  than the fetch radius is cut off at the tile edge and its area comes out too
  small — observed roughly a quarter short on a long field parcel. **Always pass
  `cover_bbox=parcel["bbox"]`.** `validate_parcel()` reports `complete`;
  a truncated parcel still cross-checks at 0.0000 % because both layers are cut
  at the same edge, so completeness is a *separate* test from consistency.
- Parcels are stitched across tile seams with `unary_union` — never read a
  parcel from a single tile.
- Invalid rings occur; repair with `buffer(0)` rather than dropping them.
- Keep every land-use class in the fetched block. Filtering them by the radius
  un-covers the far end of a long parcel and makes validation radius-dependent.

### How the geometry was verified

- **Cross-layer:** parcel outlines come from the `gst` layer, land-use patches
  from `nfl` — independent layers that must partition each parcel exactly.
  14/14 parcels reproduced their area to **0.0000 %**.
- **Topology:** 99 % of boundary vertices in a tile are shared bit-identically by
  two or more parcels — independent per-feature simplification would destroy that.
- Known limitation: `nfl` does not always classify every patch (a few hundred m²
  unclassified inside a ~9 ha field was observed), so `validate_parcel` allows 1 %.

This is **cadastral reference data, not a survey**. `rstatus` `G` = Grenzkataster
(coordinates are legally binding); anything else = Grundsteuerkataster (the
boundary is indicative only). Say which one applies before anyone relies on it.

`find_parcel` also returns the **EZ (Einlagezahl)** — parcels sharing an EZ have
the same owner, and KG + EZ is exactly the reference a Grundbuchsauszug needs. The
BEV data never contains owner names.

### Existing roads as width precedent

When someone asks "is X m wide enough", the strongest answer is the width of the
municipality's own newest streets — measure the actual road *parcels* (land-use
class 95) instead of arguing from guidelines. Recipe: Voronoi medial axis (densify
the boundary at 0,5 m, `voronoi_diagram`, keep edges `within` the polygon,
`linemerge`), then local width = 2 × distance from each axis point to the boundary.
Traps: road parcels are **branched networks** — analyse every branch, not the
longest merged path (a first pass once measured one 37-m branch of a 266-m
network); trim samples within ~3,5 m of dead ends (end caps shrink the width
artificially); junction bulges are turning areas, not the corridor width. Check
the parcels really are public land before citing them. Parcel width ≠ asphalt
width; the cadastre cannot see the kerb.

## Laying out a development on a real plot

When the job is "what fits here", the drawing is the *last* step. Work the numbers
first, and show them alongside the plan.

**Work in a plot-aligned frame.** Build unit vectors from one real boundary edge
and express the layout as `(u, v)` = metres across / along that edge. Cadastral
parcels are skewed quadrilaterals; laying a design out on map axes fights the plot.

**Measure setbacks as polygon-to-polygon distances, never as axis spans.** The
single biggest trap. A boundary 2° off your layout frame drifts ~3,5 cm per metre,
so a setback sampled at mid-width is wrong at the ends: one hall showed a nominal
2,00 m and pinched to **1,15 m** at its gable. Let `checkplan.py` confirm it on the
finished DXF.

**Build a couple of centimetres of safety into statutory distances.** Writing the
plan file rounds coordinates, so a setback placed at exactly 3,00 m can land at
2,9976 m.

**Yield is a step function, so find the steps.** Buildable count jumps at discrete
widths/depths and is flat in between, so a "minimum purchase" can land in a dead
zone where land is paid for that cannot hold another unit. Sweep the parameter and
print the table. Sweep *shape* too: a fixed area bought as a deep strip or a
shallow slab does not perform the same.

**Sensitivity-test any dimension you did not measure yourself.** Cross-check
dimensions read off someone's drawing against *stated areas* on the source
document; pixel-measuring a scanned plan does not converge.

**Depth and width are separate budgets.** Anything spanning the full width (a hall
with an apron) is charged to the *depth* budget; extra width never buys it back.

**Austrian setback rules that change the answer** (Upper Austria: Oö. **BauTG 2013
§ 40 Z 1** — check the Bundesland): 3 m to any *Bauplatz- oder Nachbargrundgrenze*,
and for building parts over 9 m high at least one THIRD of their height. All of it
yields to a Bebauungsplan, so ask whether one exists.
- Two buildings on **one** plot need only a fire separation; on **separate** plots
  they need 3 m *each side* of the new line, i.e. **6 m apart**.
- A building fronting a private lane must not sit on its own plot boundary. Give
  each plot **half the lane** with a mutual right of way.

**Access roads.**
- A *sharper* corner costs **more** land than a filleted one — the miter juts out.
- Inner kerb radius = R − half width. ≥ 8 m for a rigid truck, ≥ 12 m articulated.
- Keep the **asphalt curved and the property boundary straight**: take the convex
  hull of the road land for the subdivision plan and verify `hull.contains(road)`;
  `simplify()` does not guarantee containment.
- Always list which parcels a corridor touches (`road ∩ each parcel`).
- Build offset alignments by offsetting the boundary chain **per segment** and
  filleting. When the width is split across a servitude strip, keep the axis ~5 cm
  inside it (3,95 not 4,00).
- A **fillet bulges toward the outside of the corner it cuts** by
  `R·(1/cos(θ/2) − 1)`. A sentence printed on the sheet ("parcel X is not
  touched") has to audit as exactly 0,00 m², not as "small".
- Dead ends need a turning area (T-hammer ~16 × 7 m for a 3-axle refuse vehicle —
  an assumption to confirm with the municipality). Let it double as a loading
  forecourt.

**Truck manoeuvring.** Reversing perpendicular into a gable door needs 10–12 m of
apron; loading *parallel* along a long face needs ~8 m (6 m if the forklift works
from inside).

**Pin an area cap to gross or net before solving anything**, and **expect a
sketch to be geometrically infeasible — quantify the deficit on the plan** rather
than quietly altering it.

**Separation patterns that change yield.** Directly coupled pairs (party wall)
count as one building. Garage bays **inside the footprint along the party wall**
cost zero land; an integrated double garage eats ~35–40 m² of the footprint — state
the remaining living area.

## Sharp edges already handled — don't rediscover them

- **ACI colour 7 renders white-on-white.** The render context must get
  `current_layout_properties.set_colors(bg="#FFFFFF")`, *and* the config needs
  `BackgroundPolicy.WHITE`. Both, not either.
- **Dimension text comes out ×100** unless `dimstyle.dxf.dimlfac = 1.0` is set.
- **`get_pixmap_bytes()` silently ignores the scale settings.** Always write the
  PDF first and rasterise from it (`to_png`).
- **Hatch patterns render as nothing** unless the pattern *definition* is passed:
  `set_pattern_fill(name, definition=PATTERNS[name], …)`. Pattern definitions are
  not stored in the DXF file.
- Hatch `angle` rotates on top of the pattern's own angle — ANSI31 is already 45°.
- Patterns have wildly different natural sizes; `AREA_STYLES` holds a tuned
  paper-mm scale per kind. Inspect a pattern's definition offsets
  (`ezdxf.tools.pattern.load()`) before adding a kind — **AR-CONC repeats every
  ~200 pattern units**, so it needs a paper scale of ~0.03; at a "normal" 0.4–1.6
  the repeat exceeds the hatched area and the fill renders as **nothing, silently**.
- **Single-line labels go in as TEXT, not MTEXT.** MTEXT without a column width
  renders as one line but is *measured* by ezdxf as if wrapped at a small default
  width: a 49-character heading reported 24 m of height instead of 3 m, so scale
  selection was wrong about a sheet that fitted fine.
- **You cannot check a finished plan PDF with `page.get_text()`** — the ezdxf
  backend draws glyphs as paths. Verify by rasterising.
- **Verify collisions on a zoom crop, not the sheet PNG.**
  `page.get_pixmap(clip=Rect(...), dpi=250)`; page coordinates are points.
- Building labels auto-place at the centroid; for internal detail give the building
  `"name": ""` + `"show_area": false` and place your own label.
- `areas` with `"kind": "plain"` is the only outline-without-hatch primitive.
  `buildings` are *always* hatched.
- **Round plan coordinates to 3 decimals, not 2** — at cm precision a clipped
  parcel drifts ~0.14 m² outside its parent; at mm ~0.01 m².
- Neighbour plot numbers: place them at the representative point of
  *(parcel − buildings)*, or the number lands on a footprint and reads as a typo.
- A **flat buffer cap** (`cap_style=2`) laid on a corner spills sideways.
- For a detail sheet, draw in a frame where +y is north, or it comes out mirrored.
- Do not name a working script `inspect.py` — it shadows the stdlib module.
- On Windows, run scripts that print `→ ≥ ²` with `PYTHONIOENCODING=utf-8`.
- MTEXT line breaks are `\P` — in Python source write `"\\P"`.
- **A label's `at` is its centre.**
- `checkplan.py` audits only the GEBAEUDE layer; garages/pools drawn as `areas`
  are invisible to it (deliberate — garages may legally sit on a boundary).
- **Dimensions take ANY `angle`**, not just 0/90. For `angle: 90` the offset shifts
  the base in x — negative = left/west. Two collinear split dims at one station
  overprint each other.
- **The `nfl` land-use layer can end mid-corridor while the `gst` parcels
  continue** — hatch the known street parcel's remainder from its `gst` geometry.
- **Streets have no fill** — clip overlapping ground hatches with shapely instead
  of reordering layers.
- **A PDF open in a viewer blocks the rebuild** on Windows (`PermissionError`).
  Build into a scratch directory, then copy file by file.
- `checkplan.py` also measures building-to-building distances: pin gaps at
  ≥ 3,05 m and give only the remainder to gardens.

## Guardrails

- Output is a **draft to be checked**. State the chosen scale, every computed area,
  and anything assumed or placed by eye.
- **Never invent measurements.** A plan that looks right but is dimensionally
  wrong is worse than no plan.
- **Draw for the audience.** A sheet for neighbours' signatures or a municipality
  carries different content than a design plan. Ask who the sheet is for — that
  decides the extent, and the extent decides the scale.
- **Say what a figure includes**, and **distinguish what is forced from what you
  chose** (road widths, radii, gaps are usually drafting picks, not regulations).
- Land use first: a plot classed *Äcker, Wiesen oder Weiden* needs rezoning before
  any of this is buildable. Check the `nfl` class up front.
- This produces a **drawing, not a survey** — no surveyor's certification.
- DXF only; for `.dwg` convert with the free ODA File Converter.
