"""Austrian cadastre access (BEV) — find a plot by number and pull its surroundings.

Two public BEV services, both free, no key, CC-BY 4.0, updated daily:

  1. Search / locator  https://kataster.bev.gv.at/at.gv.bev.kataster/api/all/
       ?term=<text>&layers=<A-B-C>        (layer codes joined with HYPHENS)
     Returns GeoJSON with KG number, Grundstücksnummer, Einlagezahl and a
     BOUNDING BOX — a locator, not real geometry.

  2. Vector tiles      https://kataster.bev.gv.at/tiles/kataster/{z}/{x}/{y}.pbf
     Mapbox Vector Tiles, EPSG:3857. Zoom 16 is the deepest level and carries
     the survey geometry (extent 65536 => ~6 mm ground quantisation). Lower
     zooms are generalised — always use z16.

Attribution required: "Datenquelle: BEV — data.bev.gv.at (CC BY 4.0)".
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from collections import defaultdict

import mapbox_vector_tile
from pyproj import Transformer
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import unary_union

SEARCH_URL = "https://kataster.bev.gv.at/at.gv.bev.kataster/api/all/"
TILE_URL = "https://kataster.bev.gv.at/tiles/kataster/{z}/{x}/{y}.pbf"
ZOOM = 16                       # deepest level; anything less is generalised
WORLD = 20037508.342789244
UA = {"User-Agent": "siteplan/1.0 (+https://github.com/F3S0J/siteplan)",
      "Accept": "application/json"}

SEARCH_LAYERS = "ADR-BL-EZ-FP-GP-GST-KG-PB-PG-STR-RN-CRD"

# search result objectType -> meaning
OBJECT_TYPES = {
    "3": "Politische Gemeinde", "4": "Katastralgemeinde", "5": "Grundstück",
    "8": "Einlagezahl", "11": "Adresse", "12": "Straße", "7701": "Ortschaft",
}

# Austrian Gauss-Krueger zone by the KG's meridian
MERIDIAN_EPSG = {28: 31254, 31: 31255, 34: 31256}

# Nutzungsart (land-use) codes, as published by the BEV Kataster application
NUTZUNG = {
    40: "Landwirtschaftlich genutzt", 41: "Gebäude", 42: "Befestigte Fläche",
    48: "Äcker, Wiesen oder Weiden", 52: "Gärten", 53: "Weingärten",
    54: "Alpen", 55: "Krummholzflächen", 56: "Wald", 57: "Gebüschflächen",
    58: "Forststraßen", 59: "Fließende Gewässer", 60: "Stehende Gewässer",
    61: "Feuchtgebiete", 62: "Vegetationsarme Flächen", 63: "Betriebsflächen",
    64: "Gewässerrandflächen", 65: "Straßenrandflächen", 72: "Friedhöfe",
    73: "Rechtlich Nicht Wald", 74: "Rechtlich Wald", 77: "Rechtlich Weingarten",
    78: "Rechtlich Nicht Weingarten", 83: "Nebenflächen",
    84: "Abbau-, Halden- und Deponieflächen", 87: "Fels- und Geröllflächen",
    88: "Gletscher", 92: "Bahnflächen", 95: "Straßenverkehrsanlagen",
    96: "Erholungsflächen",
}
ROAD_CODES = {95, 92, 58}       # existing traffic infrastructure
BUILDING_CODES = {41}


def _get(url: str, timeout=60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------- search
def search(term: str, layers: str = SEARCH_LAYERS) -> list[dict]:
    """Free-text search over the Austrian cadastre. Returns GeoJSON features."""
    url = f"{SEARCH_URL}?term={urllib.parse.quote(term)}&layers={layers}"
    data = json.loads(_get(url))
    return data.get("data", {}).get("features", [])


def find_kg(name: str) -> dict | None:
    """Look up a Katastralgemeinde by name -> kg_nr, meridian, Bundesland."""
    for f in search(name, layers="KG"):
        p = f["properties"]
        if str(p.get("objectType")) == "4":
            return {
                "kg": p["kg"], "kg_nr": p["kg_nr"], "gkz": p.get("gkz"),
                "meridian": p.get("meridian"), "bl": p.get("bl"),
                "bbox": _bbox(f["geometry"]),
            }
    return None


def find_parcel(kg_nr: str | int, gnr: str) -> dict | None:
    """Locate a Grundstück by Katastralgemeinde number + Grundstücksnummer."""
    for f in search(f"{kg_nr} {gnr}", layers="GST"):
        p = f["properties"]
        if str(p.get("objectType")) == "5" and str(p.get("gnr")) == str(gnr):
            x0, y0, x1, y1 = _bbox(f["geometry"])
            return {
                "kg_nr": str(p.get("kg")), "gnr": str(p["gnr"]),
                "ez": p.get("ez"), "rstatus": p.get("rstatus"),
                "bbox": (x0, y0, x1, y1),
                "center": ((x0 + x1) / 2, (y0 + y1) / 2),
            }
    return None


def _bbox(geom) -> tuple[float, float, float, float]:
    g = shape(geom)
    return g.bounds


# ---------------------------------------------------------------- tile access
def _deg2tile(lon, lat, z):
    n = 2 ** z
    return (int((lon + 180.0) / 360.0 * n),
            int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n))


def _repair(poly):
    """Self-intersecting rings do occur in the tiles. Repair them instead of
    dropping them — a discarded patch silently loses area."""
    if poly.is_empty:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
        if poly.is_empty or not poly.is_valid:
            return None
    return poly if poly.area > 0 else None


def _tile_rings(feature, z, x, y, extent, to_local):
    g = feature["geometry"]
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    n = 2 ** z
    span = 2 * WORLD / n
    ox, oy = -WORLD + x * span, WORLD - (y + 1) * span
    out = []
    for poly in polys:
        rings = []
        for ring in poly:
            rings.append([to_local(ox + px / extent * span, oy + py / extent * span)
                          for px, py in ring])
        out.append(rings)
    return out


def fetch_context(lon: float, lat: float, radius_m: float = 250.0,
                  meridian: int = 31, origin: tuple | None = None,
                  cover_bbox: tuple | None = None) -> dict:
    """Download every cadastral parcel and land-use area within `radius_m` of a
    point, reprojected to Austrian Gauss-Krueger metres and shifted so that
    `origin` (default: the given point) becomes (0, 0).

    Parcels split across tile borders are stitched back together.

    `cover_bbox` (minlon, minlat, maxlon, maxlat) forces the fetch to span at
    least that box — pass the focus parcel's bbox from find_parcel(), otherwise
    a parcel larger than the radius is silently CUT OFF at the tile edge and its
    area comes out too small. One tile of margin is always added so that
    features touching the requested area are still complete.
    """
    epsg = MERIDIAN_EPSG.get(meridian, 31255)
    to_gk = Transformer.from_crs("EPSG:3857", f"EPSG:{epsg}", always_xy=True)
    wgs_to_gk = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    ox, oy = wgs_to_gk.transform(*(origin or (lon, lat)))

    def to_local(mx, my):
        gx, gy = to_gk.transform(mx, my)
        return (gx - ox, gy - oy)

    # tile range covering the radius AND any bbox we were told to cover
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    minlon, minlat = lon - dlon, lat - dlat
    maxlon, maxlat = lon + dlon, lat + dlat
    if cover_bbox:
        minlon, minlat = min(minlon, cover_bbox[0]), min(minlat, cover_bbox[1])
        maxlon, maxlat = max(maxlon, cover_bbox[2]), max(maxlat, cover_bbox[3])

    tx0, ty1 = _deg2tile(minlon, minlat, ZOOM)
    tx1, ty0 = _deg2tile(maxlon, maxlat, ZOOM)
    # one tile of margin: features on the edge of the area of interest stay whole
    x0, x1 = min(tx0, tx1) - 1, max(tx0, tx1) + 1
    y0, y1 = min(ty0, ty1) - 1, max(ty0, ty1) + 1

    parcel_parts: dict[tuple, list] = defaultdict(list)
    parcel_meta: dict[tuple, dict] = {}
    landuse_parts: dict[int, list] = defaultdict(list)
    kg_polys: list[tuple] = []      # (properties, polygon) — pick the one at the origin
    tiles = 0

    for tx in range(min(x0, x1), max(x0, x1) + 1):
        for ty in range(min(y0, y1), max(y0, y1) + 1):
            try:
                raw = _get(TILE_URL.format(z=ZOOM, x=tx, y=ty))
            except Exception:
                continue
            tiles += 1
            tile = mapbox_vector_tile.decode(raw)

            kg_layer = tile.get("kg")
            if kg_layer:
                for f in kg_layer["features"]:
                    for rings in _tile_rings(f, ZOOM, tx, ty, kg_layer["extent"], to_local):
                        poly = Polygon(rings[0], rings[1:])
                        if poly.is_valid:
                            kg_polys.append((dict(f["properties"]), poly))

            layer = tile.get("gst")
            if layer:
                ext = layer["extent"]
                for f in layer["features"]:
                    p = f["properties"]
                    key = (str(p.get("kg")), str(p.get("gnr")))
                    parcel_meta[key] = p
                    for rings in _tile_rings(f, ZOOM, tx, ty, ext, to_local):
                        poly = _repair(Polygon(rings[0], rings[1:]))
                        if poly is not None:
                            parcel_parts[key].append(poly)

            layer = tile.get("nfl")
            if layer:
                ext = layer["extent"]
                for f in layer["features"]:
                    ns = int(f["properties"].get("ns", 0))
                    for rings in _tile_rings(f, ZOOM, tx, ty, ext, to_local):
                        poly = _repair(Polygon(rings[0], rings[1:]))
                        if poly is not None:
                            landuse_parts[ns].append(poly)

    # tiles snap to ~410 m at this latitude, so the fetch always overshoots the
    # requested radius. Keep whole features that INTERSECT the disc — never cut a
    # parcel in half, that would falsify its area.
    disc = Point(0, 0).buffer(radius_m, quad_segs=64)

    # outline of everything we actually downloaded — anything touching it is cut off
    span = 2 * WORLD / 2 ** ZOOM
    bx0, bx1 = -WORLD + x0 * span, -WORLD + (x1 + 1) * span
    by0, by1 = WORLD - (y1 + 1) * span, WORLD - y0 * span
    edge = []
    n = 24
    for i in range(n):                                    # densify: the block is
        edge.append((bx0 + (bx1 - bx0) * i / n, by0))     # not a rectangle after
    for i in range(n):                                    # reprojection
        edge.append((bx1, by0 + (by1 - by0) * i / n))
    for i in range(n):
        edge.append((bx1 - (bx1 - bx0) * i / n, by1))
    for i in range(n):
        edge.append((bx0, by1 - (by1 - by0) * i / n))
    block = Polygon([to_local(mx, my) for mx, my in edge])

    parcels = []
    for key, parts in parcel_parts.items():
        merged = unary_union(parts)           # stitch tile seams
        if not merged.intersects(disc):
            continue
        p = parcel_meta[key]
        parcels.append({
            "kg": key[0], "gnr": key[1], "ez": p.get("ez"),
            "rstatus": p.get("rstatus"),
            "area_m2": merged.area,
            # False => the parcel runs past the downloaded area and its area is
            # a LOWER BOUND, not the real one. Re-fetch with a bigger cover_bbox.
            "complete": merged.distance(block.exterior) > 0.5,
            "geometry": merged,
        })
    parcels.sort(key=lambda d: -d["area_m2"])

    landuse = []
    for ns, parts in landuse_parts.items():
        merged = unary_union(parts)
        within = merged.intersection(disc)
        # Every class in the block is kept. Dropping the ones that miss the disc
        # would silently un-cover the far end of a parcel longer than the radius,
        # making validate_parcel() radius-dependent.
        # geometry stays UNCLIPPED (it is fed to CAD and used for validation);
        # only the reported statistic is limited to the requested radius.
        landuse.append({
            "ns": ns, "name": NUTZUNG.get(ns, f"Code {ns}"),
            "area_m2": within.area, "area_full_m2": merged.area,
            "geometry": merged,
        })
    landuse.sort(key=lambda d: -d["area_m2"])

    # the Katastralgemeinde actually containing the query point
    kg_info: dict = {}
    origin_pt = Point(0, 0)
    for props, poly in kg_polys:
        if poly.contains(origin_pt):
            kg_info = props
            break
    if not kg_info and kg_polys:
        kg_info = min(kg_polys, key=lambda t: t[1].distance(origin_pt))[0]

    return {
        "epsg": epsg, "origin_gk": (ox, oy), "origin_wgs": (lon, lat),
        "radius_m": radius_m, "tiles": tiles, "kg": kg_info,
        "parcels": parcels, "landuse": landuse,
        "attribution": "Datenquelle: BEV — data.bev.gv.at (CC BY 4.0)",
    }


def validate_parcel(ctx: dict, gnr: str) -> dict:
    """Cross-check one parcel against an independent layer.

    In the Austrian cadastre the Nutzungsflächen (nfl layer) partition each
    parcel exactly. The parcel outline comes from the `gst` layer, the land-use
    patches from `nfl` — two separate tile layers. If the land-use patches
    clipped to the parcel reproduce its area, both layers are internally
    consistent and the geometry survived the tile -> metres pipeline intact.
    """
    p = next((q for q in ctx["parcels"] if q["gnr"] == str(gnr)), None)
    if p is None:
        return {"ok": False, "reason": f"parcel {gnr} not in context"}
    poly = p["geometry"]
    covered = unary_union(
        [lu["geometry"].intersection(poly) for lu in ctx["landuse"]]
    )
    dev = (covered.area - poly.area) / poly.area if poly.area else 0
    return {
        # Most parcels reproduce to 0.0000%, but the nfl layer does not always
        # classify every last patch (observed: one unclassified 383 m² area in an
        # 8.9 ha field), so allow 1%. Anything worse means a real problem.
        # NOTE: a truncated parcel still cross-checks perfectly, because both
        # layers are cut at the same tile edge — completeness is a separate test.
        "ok": abs(dev) < 0.01 and p.get("complete", True),
        "complete": p.get("complete", True),
        "gnr": str(gnr),
        "parcel_area_m2": poly.area,
        "landuse_area_m2": covered.area,
        "deviation_pct": dev * 100,
        "vertices": len(poly.exterior.coords) - 1 if poly.geom_type == "Polygon"
        else sum(len(g.exterior.coords) - 1 for g in poly.geoms),
    }


# ----------------------------------------------------- export to siteplan
def _rings(geom, simplify=0.0):
    """shapely geometry -> list of exterior rings as [[x,y], ...]"""
    if simplify:
        geom = geom.simplify(simplify, preserve_topology=True)
    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    return [[[round(x, 3), round(y, 3)] for x, y in p.exterior.coords[:-1]]
            for p in polys if p.geom_type == "Polygon"]


def to_siteplan(ctx: dict, focus_gnr: str | None = None, meta: dict | None = None,
                include_landuse=True, max_neighbours=60,
                extent_m: float | None = None) -> dict:
    """Turn a fetched context into a siteplan plan dict.

    The focus parcel is drawn whole. Everything else is clipped to `extent_m`
    around the origin (default: the fetch radius) so the sheet frames the plot
    and its neighbourhood instead of the whole tile.
    """
    e = extent_m or ctx["radius_m"]
    clip = box(-e, -e, e, e)      # square reads as a map extract; a disc leaves arcs
    # a plot narrower than ~1/60 of the sheet extent cannot hold its own number
    label_min_m = e * 2 / 60.0

    def clipped(geom):
        g = geom.intersection(clip)
        return None if g.is_empty else g
    plan: dict = {
        "sheet": "A3", "orientation": "auto", "scale": None,
        "meta": meta or {},
        "parcels": [], "buildings": [], "areas": [], "labels": [],
        "dimensions": "auto",
    }

    focus = None
    if focus_gnr:
        focus = next((p for p in ctx["parcels"] if p["gnr"] == str(focus_gnr)), None)

    if focus:
        for ring in _rings(focus["geometry"]):
            plan["parcels"].append({
                "nr": focus["gnr"], "points": ring, "show_area": True,
            })

    # neighbouring parcels: outline only, number at the centroid
    for p in ctx["parcels"][:max_neighbours]:
        if focus and p["gnr"] == focus["gnr"]:
            continue
        g = clipped(p["geometry"])
        if g is None:
            continue
        for ring in _rings(g):
            plan["areas"].append({"kind": "plain", "points": ring})
        # Only label parcels big enough for the number to sit inside them at the
        # scale this sheet will end up at. Dense village plots otherwise turn the
        # drawing into a pile of overlapping text.
        b = g.bounds
        if min(b[2] - b[0], b[3] - b[1]) >= label_min_m:
            c = g.representative_point()
            plan["labels"].append({"text": p["gnr"],
                                   "at": [round(c.x, 2), round(c.y, 2)], "size": 1.8})

    if include_landuse:
        for lu in ctx["landuse"]:
            g = clipped(lu["geometry"])
            if g is None:
                continue
            if lu["ns"] in BUILDING_CODES:
                for ring in _rings(g):
                    if len(ring) >= 3:
                        plan["buildings"].append({"points": ring, "show_area": False,
                                                  "hatch": "ANSI31"})
            elif lu["ns"] in ROAD_CODES:
                for ring in _rings(g):
                    if len(ring) >= 3:
                        plan["areas"].append({"kind": "gravel", "points": ring})

    return plan


def summarise(ctx: dict, focus_gnr: str | None = None) -> str:
    lines = []
    kg = ctx["kg"]
    if kg:
        lines.append(f"KG {kg.get('kg')} ({kg.get('kg_nr')}), {kg.get('bl')}, "
                     f"Meridian M{kg.get('meridian')} -> EPSG:{ctx['epsg']}")
    lines.append(f"{len(ctx['parcels'])} parcels, {len(ctx['landuse'])} land-use "
                 f"classes, from {ctx['tiles']} tiles, r={ctx['radius_m']:.0f} m")
    if focus_gnr:
        f = next((p for p in ctx["parcels"] if p["gnr"] == str(focus_gnr)), None)
        if f:
            lines.append(f"focus parcel {f['gnr']}: {f['area_m2']:,.0f} m² "
                         f"(EZ {f['ez']}, Status {f['rstatus']})")
        else:
            lines.append(f"focus parcel {focus_gnr}: NOT FOUND in this radius")
    lines.append("\nland use in the area:")
    for lu in ctx["landuse"][:10]:
        flag = "  <- road" if lu["ns"] in ROAD_CODES else (
            "  <- building" if lu["ns"] in BUILDING_CODES else "")
        lines.append(f"  {lu['ns']:>3} {lu['name']:<34}{lu['area_m2']:>12,.0f} m²{flag}")
    lines.append("\n" + ctx["attribution"])
    return "\n".join(lines)
