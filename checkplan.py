"""Re-measure a FINISHED plan by reading its DXF back.

The point is to check the drawing that will actually be handed over, not the
variables the generator happened to hold. Every real error found so far — a
setback that looked fine in the code and pinched at a skewed boundary, a detail
sheet that had silently gone stale, a building sitting on a plot line — was
invisible from inside the generator and obvious from here.

    python checkplan.py site.dxf                     # inventory + clearances
    python checkplan.py site.dxf --setback 3.0       # flag anything closer
    python checkplan.py site.dxf --parent parent.json  # containment check

Importable too:

    from checkplan import layers, sides, report
    polys = layers("site.dxf")            # {layer: [shapely Polygon, ...]}
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib

import ezdxf
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

BUILDING_LAYERS = ("GEBAEUDE",)
BOUNDARY_LAYERS = ("GRENZE",)


def layers(path) -> dict[str, list[Polygon]]:
    """Every closed LWPOLYLINE in the DXF, grouped by layer."""
    doc = ezdxf.readfile(str(path))
    out: dict[str, list[Polygon]] = {}
    for e in doc.modelspace().query("LWPOLYLINE"):
        pts = [(round(p[0], 4), round(p[1], 4)) for p in e.get_points("xy")]
        # drop consecutive duplicates — cadastral rings often carry them
        clean = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        if len(clean) < 3:
            continue
        poly = Polygon(clean)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area <= 0:
            continue
        out.setdefault(e.dxf.layer, []).append(poly)
    return out


def sides(p: Polygon) -> list[float]:
    """Edge lengths, sorted — the quickest way to confirm a stated size."""
    c = list(p.exterior.coords)[:-1]
    return sorted(round(math.dist(c[i], c[(i + 1) % len(c)]), 2)
                  for i in range(len(c)))


TOL = 0.001          # 1 mm: a setback of exactly 3,00 m must not fail on float noise


def _fmt(a: float) -> str:
    return f"{a:,.1f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def report(path, setback: float | None = None, parent: str | None = None,
           building_layers=BUILDING_LAYERS, boundary_layers=BOUNDARY_LAYERS):
    path = pathlib.Path(path)
    L = layers(path)
    problems: list[str] = []
    print(f"== {path.name}")

    for name in sorted(L):
        tot = sum(p.area for p in L[name])
        print(f"   {name:<12} {len(L[name]):>3} Polygone   {_fmt(tot):>12} m² gesamt")

    bounds = [p for lay in boundary_layers for p in L.get(lay, [])]
    blds = [p for lay in building_layers for p in L.get(lay, [])]

    if bounds:
        parcel = max(bounds, key=lambda p: p.area)
        print(f"\n   Bezugsfläche (größtes Polygon auf {'/'.join(boundary_layers)}): "
              f"{_fmt(parcel.area)} m²")
    else:
        parcel = None

    if blds:
        # Buildings lying entirely outside the reference parcel are context taken
        # from the cadastre (the neighbours' houses). They are not part of the
        # project and must not be measured against its setbacks.
        inside = [b for b in blds if parcel is None or b.intersects(parcel)]
        context = len(blds) - len(inside)
        print(f"\n   {'Gebäude':<10}{'Fläche':>11}  {'Seiten':<24}"
              f"{'-> Grenze':>11}{'-> Nachbar':>12}")
        for i, b in enumerate(sorted(inside, key=lambda p: -p.area), 1):
            others = [o for o in inside if o is not b]
            d_bound = parcel.exterior.distance(b) if parcel is not None else float("nan")
            d_near = min((b.distance(o) for o in others), default=float("nan"))
            s = "×".join(f"{v:g}" for v in sides(b)[::2]) if len(sides(b)) == 4 \
                else f"{len(sides(b))} Ecken"
            print(f"   {i:<10}{_fmt(b.area):>11}  {s:<24}"
                  f"{d_bound:>11.2f}{d_near:>12.2f}")
            if setback is not None:
                if parcel is not None and d_bound < setback - TOL:
                    problems.append(f"Gebäude {i}: nur {d_bound:.2f} m zur Grenze "
                                    f"(gefordert {setback:.2f} m)")
                if others and d_near < setback - TOL:
                    problems.append(f"Gebäude {i}: nur {d_near:.2f} m zum Nachbargebäude "
                                    f"(gefordert {setback:.2f} m)")
            if parcel is not None and b.difference(parcel).area > 0.01:
                problems.append(f"Gebäude {i} ragt {b.difference(parcel).area:.2f} m² "
                                f"über die Bezugsfläche hinaus")
        if context:
            print(f"   (+ {context} Bestandsgebäude außerhalb der Bezugsfläche — "
                  f"Kontext, nicht geprüft)")

    # does the drawn parcel sit inside the parcel it was cut from?
    if parent and parcel is not None:
        g = shape(json.loads(pathlib.Path(parent).read_text()))
        out = parcel.difference(g).area
        print(f"\n   außerhalb der Mutterfläche: {out:.4f} m²")
        # 1 cm coordinate rounding on a few hundred metres of perimeter is normal
        if out > 0.5:
            problems.append(f"Teilfläche liegt {out:.2f} m² außerhalb der Mutterfläche")

    print()
    if problems:
        for p in problems:
            print(f"   [XX] {p}")
        print(f"\n   {len(problems)} Beanstandung(en)")
    else:
        print("   [OK] keine Beanstandungen")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dxf")
    ap.add_argument("--setback", type=float, default=None,
                    help="minimum distance to boundaries and neighbours, in metres")
    ap.add_argument("--parent", default=None,
                    help="GeoJSON geometry of the parcel the plot was cut from")
    a = ap.parse_args()
    raise SystemExit(1 if report(a.dxf, a.setback, a.parent) else 0)


if __name__ == "__main__":
    main()
