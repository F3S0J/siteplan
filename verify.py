"""Round-trip check: does the generated DXF still hold true metres, and does the
PDF print at the declared scale? Run after changing siteplan.py.

    python verify.py example/musterdorf.json
"""
import json
import pathlib
import sys

import ezdxf
import pymupdf
from ezdxf.math import area as polygon_area

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from siteplan import generate, sheet_size  # noqa: E402

PT_TO_MM = 25.4 / 72.0


def main(plan_file):
    plan_file = pathlib.Path(plan_file)
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    res = generate(plan_file, verbose=False)
    scale = res["scale"]
    ok = True

    print(f"plot scale: 1:{scale}\n")

    # 1 -- DXF geometry must still be the raw metre coordinates from the JSON
    doc = ezdxf.readfile(res["dxf"])
    msp = doc.modelspace()
    print(f"DXF units      : {doc.units} (6 = metres)")
    ok &= doc.units == 6

    want = [tuple(p) for p in plan["parcels"][0]["points"]]
    got = None
    for pl in msp.query('LWPOLYLINE[layer=="GRENZE"]'):
        got = [(round(p[0], 6), round(p[1], 6)) for p in pl.get_points("xy")]
        break
    match = got == [(float(a), float(b)) for a, b in want]
    print(f"parcel vertices: {'unchanged' if match else 'MISMATCH'}  {got}")
    ok &= match

    a_json = abs(polygon_area(want))
    print(f"parcel area    : {a_json:,.1f} m² (from DXF geometry)")

    # 2 -- PDF must print at the declared scale
    sheet_w, sheet_h = sheet_size(plan)
    page = pymupdf.open(res["pdf"])[0]
    pw, ph = page.rect.width * PT_TO_MM, page.rect.height * PT_TO_MM
    print(f"\nsheet          : {pw:.1f} x {ph:.1f} mm (nominal {sheet_w:g} x {sheet_h:g})")
    ok &= abs(pw - sheet_w) < 1.0 and abs(ph - sheet_h) < 1.0

    xs = [p[0] for p in want]
    ys = [p[1] for p in want]
    expect_w = (max(xs) - min(xs)) * 1000 / scale
    expect_h = (max(ys) - min(ys)) * 1000 / scale

    # locate the red parcel boundary on the page and measure it
    found = None
    for d in page.get_drawings():
        col = d.get("color")
        if col and col[0] > 0.8 and col[1] < 0.3 and col[2] < 0.3:
            found = d["rect"]
            break
    if found:
        got_w, got_h = found.width * PT_TO_MM, found.height * PT_TO_MM
        dw, dh = abs(got_w - expect_w), abs(got_h - expect_h)
        print(f"parcel on paper: {got_w:.2f} x {got_h:.2f} mm  "
              f"(expected {expect_w:.2f} x {expect_h:.2f})")
        print(f"deviation      : {dw:.2f} / {dh:.2f} mm "
              f"({dw / expect_w:.3%} / {dh / expect_h:.3%})")
        ok &= dw < 0.5 and dh < 0.5
    else:
        print("parcel on paper: NOT FOUND (boundary colour changed?)")
        ok = False

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1
                          else "example/musterdorf.json"))
