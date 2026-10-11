"""Worked example: plot number in -> scaled cadastral site plan out.

    python example/from_kataster.py <Katastralgemeinde> <Grundstücksnummer> [radius_m] [date]

Pulls the real parcel boundary, its neighbours, building footprints and
existing roads from the public BEV cadastre (CC BY 4.0) and builds a plan
in the current directory.
"""
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import kataster as K            # noqa: E402
from siteplan import generate   # noqa: E402

if len(sys.argv) < 3:
    raise SystemExit(__doc__)

kg_name = sys.argv[1]
gnr = sys.argv[2]
radius = float(sys.argv[3]) if len(sys.argv) > 3 else 180.0
date = sys.argv[4] if len(sys.argv) > 4 else datetime.date.today().strftime("%d.%m.%Y")

out = pathlib.Path.cwd()

kg = K.find_kg(kg_name)
if not kg:
    raise SystemExit(f"Katastralgemeinde {kg_name!r} not found")
parcel = K.find_parcel(kg["kg_nr"], gnr)
if not parcel:
    raise SystemExit(f"Grundstück {gnr} not found in KG {kg['kg_nr']}")

ctx = K.fetch_context(*parcel["center"], radius_m=radius, meridian=kg["meridian"],
                      cover_bbox=parcel["bbox"])   # never truncate the focus parcel
print(K.summarise(ctx, focus_gnr=gnr))

check = K.validate_parcel(ctx, gnr)
print(f"\ncross-layer check: {check['parcel_area_m2']:,.1f} m² outline vs "
      f"{check['landuse_area_m2']:,.1f} m² land-use "
      f"({check['deviation_pct']:+.4f}%)")
print(f"complete (not cut off by the download extent): {check['complete']}")
print(f"verdict: {'OK' if check['ok'] else 'NOT TRUSTWORTHY'}")

plan = K.to_siteplan(ctx, focus_gnr=gnr, meta={
    "project": f"KG {kg['kg']} ({kg['kg_nr']}) — Grst. Nr. {gnr}",
    "subtitle": f"Bestandsplan aus dem Kataster · Umgebung r={radius:.0f} m",
    "date": date,
    "plan_no": f"KAT-{kg['kg_nr']}-{gnr.replace('/', '-')}",
    "drawn_by": "BEV (CC BY 4.0)",
})
stem = f"kataster_{kg['kg_nr']}_{gnr.replace('/', '-')}"
plan["output"] = stem
(out / f"{stem}.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
print(f"\nfocus parcels {len(plan['parcels'])} | neighbours {len(plan['areas'])} | "
      f"buildings {len(plan['buildings'])} | labels {len(plan['labels'])}\n")
generate(out / f"{stem}.json", outdir=out)
