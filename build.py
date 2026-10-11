"""CLI: build a scaled site plan (DXF + PDF + PNG) from a JSON plan file.

    python build.py plan.json                 # auto scale, next to the plan file
    python build.py plan.json -o C:/out       # choose an output folder
    python build.py plan.json --scale 500     # force a plot scale
    python build.py plan.json --no-png        # skip the QA raster
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from siteplan import generate  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Generate a scaled CAD site plan.")
    ap.add_argument("plan", help="JSON plan file")
    ap.add_argument("-o", "--outdir", default=None, help="output folder")
    ap.add_argument("--scale", type=int, default=None,
                    help="force plot scale denominator, e.g. 500 for 1:500")
    ap.add_argument("--no-png", action="store_true", help="skip the QA PNG")
    args = ap.parse_args()

    if args.scale:
        import json
        p = pathlib.Path(args.plan)
        plan = json.loads(p.read_text(encoding="utf-8"))
        plan["scale"] = args.scale
        plan.setdefault("output", p.stem)   # keep the original file stem
        tmp = p.with_suffix(".scaled.json")
        tmp.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        try:
            generate(tmp, args.outdir, png=not args.no_png)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        generate(args.plan, args.outdir, png=not args.no_png)


if __name__ == "__main__":
    main()
