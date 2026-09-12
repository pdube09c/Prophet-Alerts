"""Generate sports/data/cfb_crosswalk.json from the TypeScript crosswalks.

The hand-verified name mappings live in the SIBLING repo
`odds-backtest-verification` (src/ncaaf-crosswalk.ts, src/cfbd-crosswalk.ts) and
that repo stays the single source of truth. Prophet-Alerts runs Python in GitHub
Actions, where that repo is not checked out, so the mappings are compiled to a
checked-in JSON data file instead of being read at runtime — CI gets no
cross-repo dependency on the live alert path.

The copy cannot diverge silently: tests/test_cfb.py re-runs this
generator whenever the source repo is present and fails if the checked-in JSON
differs. When the .ts files change (a reclassifier retag, a new team), re-run:

    python -m tools.gen_cfb_crosswalk

and commit the regenerated JSON.

Parsing is deliberately narrow: it reads the two `export const X: Record<...>`
object literals as literal key/value string pairs. It is NOT a TypeScript
parser — anything it cannot read as a flat string->string mapping is a hard
error, never a skipped entry, so a silently-dropped team is impossible.
"""

from __future__ import annotations

import argparse
import json
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_SRC = os.path.join(os.path.dirname(_ROOT), "odds-backtest-verification", "src")
OUT_PATH = os.path.join(_ROOT, "sports", "data", "cfb_crosswalk.json")

# One `key: "value",` entry. The key is either a double-quoted string or a bare
# JS identifier (the .ts files use both: "App State" vs Towson). A trailing
# `// hand-resolved` comment is allowed and ignored.
_ENTRY = re.compile(
    r'^\s*(?:"(?P<qkey>[^"]+)"|(?P<bkey>[A-Za-z_$][\w$]*))\s*:\s*'
    r'"(?P<value>[^"]*)"\s*,?\s*(?://.*)?$'
)
# A `const NAME: ReadonlySet<...> = new Set<...>([ "a", "b" ]);` literal.
_SET_ITEM = re.compile(r'^\s*"(?P<value>[^"]+)"\s*,?\s*(?://.*)?$')


def _read(src_dir: str, name: str) -> str:
    path = os.path.join(src_dir, name)
    if not os.path.exists(path):
        raise SystemExit(
            f"crosswalk source not found: {path}\n"
            f"Pass --src /path/to/odds-backtest-verification/src, or clone that "
            f"repo as a sibling of this one.")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _body(text: str, decl: str, source: str) -> list[str]:
    """The lines between `<decl>` and its closing `};` / `]);`."""
    start = text.find(decl)
    if start < 0:
        raise SystemExit(f"{source}: could not find declaration {decl!r}")
    rest = text[start + len(decl):]
    end = re.search(r"^\s*(?:\}|\])", rest, re.M)
    if end is None:
        raise SystemExit(f"{source}: unterminated literal after {decl!r}")
    return rest[: end.start()].splitlines()


def _parse_record(text: str, decl: str, source: str) -> dict:
    """Parse an object literal into a dict. Any unreadable line is fatal."""
    out: dict = {}
    for line in _body(text, decl, source):
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("/*") \
                or stripped.startswith("*"):
            continue
        m = _ENTRY.match(line)
        if m is None:
            raise SystemExit(
                f"{source}: cannot parse crosswalk entry (refusing to skip it):\n"
                f"  {line.rstrip()}")
        key = m.group("qkey") if m.group("qkey") is not None else m.group("bkey")
        if key in out:
            raise SystemExit(f"{source}: duplicate key {key!r}")
        out[key] = m.group("value")
    if not out:
        raise SystemExit(f"{source}: {decl!r} parsed to zero entries")
    return out


def _parse_set(text: str, decl: str, source: str) -> list[str]:
    out: list[str] = []
    for line in _body(text, decl, source):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        m = _SET_ITEM.match(line)
        if m is None:
            raise SystemExit(
                f"{source}: cannot parse set entry (refusing to skip it):\n"
                f"  {line.rstrip()}")
        out.append(m.group("value"))
    return out


def build(src_dir: str = _DEFAULT_SRC) -> dict:
    """Compile both .ts crosswalks into the JSON payload."""
    ncaaf = _read(src_dir, "ncaaf-crosswalk.ts")
    cfbd = _read(src_dir, "cfbd-crosswalk.ts")

    conference = _parse_record(
        ncaaf, "export const CONFERENCE: Record<string, Conference> = {",
        "ncaaf-crosswalk.ts")
    cfbd_to_canonical = _parse_record(
        cfbd, "export const CFBD_TO_CANONICAL: Record<string, string> = {",
        "cfbd-crosswalk.ts")
    p4_conferences = _parse_set(
        ncaaf, "const P4_CONFERENCES: ReadonlySet<Conference> = new Set<Conference>([",
        "ncaaf-crosswalk.ts")
    p4_extra = _parse_set(
        ncaaf, "const P4_EXTRA: ReadonlySet<string> = new Set<string>([",
        "ncaaf-crosswalk.ts")

    # Every CFBD mapping must land on a name the odds crosswalk knows, or the
    # join would resolve to a team we cannot classify. Catch it at build time.
    unknown = sorted({v for v in cfbd_to_canonical.values() if v not in conference})
    return {
        "_source": "odds-backtest-verification/src/{ncaaf,cfbd}-crosswalk.ts",
        "_generator": "tools/gen_cfb_crosswalk.py",
        "_note": "GENERATED — do not hand-edit. Edit the .ts source and re-run "
                 "the generator. tests/test_cfb.py enforces this.",
        "conference": conference,
        "cfbd_to_canonical": cfbd_to_canonical,
        "p4_conferences": p4_conferences,
        "p4_extra": p4_extra,
        "_unmapped_canonical": unknown,
    }


def _serialize(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m tools.gen_cfb_crosswalk")
    ap.add_argument("--src", default=_DEFAULT_SRC,
                    help="path to odds-backtest-verification/src")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the checked-in JSON is stale")
    args = ap.parse_args()

    payload = build(args.src)
    text = _serialize(payload)
    if args.check:
        with open(OUT_PATH, encoding="utf-8") as fh:
            current = fh.read()
        if current != text:
            raise SystemExit(
                f"{OUT_PATH} is STALE relative to {args.src}.\n"
                f"Re-run: python -m tools.gen_cfb_crosswalk")
        print(f"[crosswalk] up to date ({len(payload['conference'])} odds names, "
              f"{len(payload['cfbd_to_canonical'])} CFBD names)")
        return

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"[crosswalk] wrote {OUT_PATH}\n"
          f"  odds names : {len(payload['conference'])}\n"
          f"  CFBD names : {len(payload['cfbd_to_canonical'])}\n"
          f"  unmapped   : {payload['_unmapped_canonical'] or 'none'}")


if __name__ == "__main__":
    main()
