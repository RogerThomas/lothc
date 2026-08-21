#!yeet
"""Generate assets/perf-race.svg from a perf.py results JSON file.

The animation is pure CSS `@keyframes` (no JS) so it plays natively when
GitHub renders it inline in a README — same technique as
https://github.com/inevolin/k8s-cpu-limits-analyzed/blob/main/assets/oom-live.svg.

Every `@keyframes` rule below explicitly repeats its held value at 100%.
Without that, a browser fills a missing 100% stop from the element's own
base attribute (width="0", opacity="0") and LINEARLY INTERPOLATES BACK to
it for the rest of the loop — the fill would race to the end and then
visibly drain/fade back out, not hold. Don't drop those stops.
"""

import json
from pathlib import Path
from typing import TypedDict


class ResultEntry(TypedDict):
    lib: str
    throughput: float


# Fixed color per library identity, not per finish rank — a library keeps
# its color across runs and lineups. Slots 1-7 of the validated categorical
# palette (see the `dataviz` skill's references/palette.md; slot 8, red, is
# unused), assigned in perf.py's own Lib declaration order. The three
# lothc-msgspec/lothc-pydantic/lothc-typeguard rows are deliberately NOT three
# more brand-new categorical hues — they're the same "lothc" identity at
# different decode-validation depths, so (per the skill's non-negotiables:
# "never a 9th generated hue", "color follows the entity") they take a single
# green one-hue ordinal ramp instead (lothc.JSON is the lightest/least
# processing, lothc-typeguard the darkest/heaviest). Verified via the skill's
# validate_palette.js validateOrdinal(): lightness-monotone, single-hue, and
# clears the light-surface 2:1 contrast floor at its lightest step (#00c200 ->
# 2.35:1) — dark mode wasn't checked since this SVG has no dark variant at all
# (confirmed: no prefers-color-scheme/media query anywhere in this file).
_lib_colors = {
    "httpx": "#2a78d6",
    "httpx_h2": "#2a78d6",
    "httpx2": "#1baf7a",
    "pyreqwest": "#4a3aa7",
    "aiohttp": "#e87ba4",
    "niquests": "#eb6834",
    "aiosonic": "#eda100",
    "lothc": "#00c200",
    "lothc-msgspec": "#009e00",
    "lothc-pydantic": "#007a00",
    "lothc-typeguard": "#005200",
}

_lane_height = 36
_lane_gap = 6
_track_x = 150
_track_width = 520
_header_h = 58
_footer_h = 24
_hold_seconds = 3.0
_axis_ticks = 6


def _lib_display_rate(throughput: float) -> str:
    return f"{throughput:,.0f} req/s"


def _build_lane(
    index: int, lib: str, throughput: float, duration: float, loop: float, color: str
) -> tuple[str, str]:
    """Return (style_rules, lane_markup) for one library's lane."""
    y = _header_h + index * (_lane_height + _lane_gap)
    pct = duration / loop * 100
    style = (
        f"#bar-{lib} {{ animation: fill-{lib} {loop:g}s linear infinite; }}\n"
        f"    #badge-{lib} {{ animation: show-{lib} {loop:g}s step-end infinite; }}\n"
        f"    @keyframes fill-{lib} "
        f"{{ 0% {{ width: 0; }} {pct:.3f}% {{ width: {_track_width}px; }} "
        f"100% {{ width: {_track_width}px; }} }}\n"
        f"    @keyframes show-{lib} "
        f"{{ 0% {{ opacity: 0; }} {pct:.3f}% {{ opacity: 1; }} 100% {{ opacity: 1; }} }}"
    )
    swatch_y = y + _lane_height / 2 - 5
    name_y = y + _lane_height / 2 - 3
    rate_y = y + _lane_height / 2 + 10
    badge_y = y + _lane_height / 2 + 4
    badge_x = _track_x + _track_width + 10
    lane = f"""  <g>
    <rect x="{_track_x}" y="{y}" width="{_track_width}" height="26" rx="4" class="track" />
    <rect id="bar-{lib}" x="{_track_x}" y="{y}" height="26" rx="4" width="0" fill="{color}" />
    <rect x="20" y="{swatch_y:.1f}" width="10" height="10" rx="2" fill="{color}" />
    <text x="36" y="{name_y:.1f}" class="name">{lib}</text>
    <text x="36" y="{rate_y:.1f}" class="rate">{_lib_display_rate(throughput)}</text>
    <g id="badge-{lib}" opacity="0">
      <text x="{badge_x}" y="{badge_y:.1f}" class="badge" fill="{color}">
        &#x2713; {duration:.2f}s
      </text>
    </g>
  </g>"""
    return style, lane


def _build_axis(race_seconds: float, lane_count: int) -> str:
    axis_bottom = _header_h + lane_count * (_lane_height + _lane_gap) - _lane_gap
    lines: list[str] = []
    ticks: list[str] = []
    for i in range(_axis_ticks):
        frac = i / (_axis_ticks - 1)
        x = _track_x + frac * _track_width
        seconds = frac * race_seconds
        lines.append(
            f'    <line x1="{x:.1f}" y1="46" x2="{x:.1f}" y2="{axis_bottom}" class="grid" />'
        )
        ticks.append(
            f'    <text x="{x:.1f}" y="40" class="tick" text-anchor="middle">{seconds:.0f}s</text>'
        )
    return "\n".join(lines) + "\n" + "\n".join(ticks)


def render_svg(
    results: list[ResultEntry], race_seconds: float, total_requests: int, concurrency: int
) -> str:
    ordered = sorted(results, key=lambda r: -r["throughput"])
    slowest = min(r["throughput"] for r in results)
    loop = race_seconds + _hold_seconds

    styles: list[str] = []
    lanes: list[str] = []
    for index, r in enumerate(ordered):
        lib = r["lib"]
        throughput = r["throughput"]
        duration = race_seconds / (throughput / slowest)
        color = _lib_colors[lib]
        style, lane = _build_lane(index, lib, throughput, duration, loop, color)
        styles.append(style)
        lanes.append(lane)

    lane_count = len(ordered)
    total_height = _header_h + lane_count * (_lane_height + _lane_gap) + _footer_h + 30
    axis_bottom = _header_h + lane_count * (_lane_height + _lane_gap) - _lane_gap
    playhead_x_end = _track_x + _track_width
    race_pct = race_seconds / loop * 100

    fastest = ordered[0]
    slowest_lib = ordered[-1]
    fastest_dur = race_seconds / (fastest["throughput"] / slowest)
    styles_block = chr(10).join("    " + s for s in styles).strip()
    lanes_block = chr(10).join(lanes)
    axis_block = _build_axis(race_seconds, lane_count)
    footer = (
        f"Benchmarked with perf.py — {total_requests} requests, concurrency {concurrency}, "
        "against a tiny JSON server."
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{total_height:.0f}" \
viewBox="0 0 760 {total_height:.0f}">
  <title>{fastest["lib"]} throughput race against {lane_count - 1} other HTTP clients</title>
  <desc>{lane_count} HTTP client libraries race to the same finish line, scaled so
  the slowest ({slowest_lib["lib"]}) takes {race_seconds:g} seconds and every other
  library finishes in proportion to how much faster it actually was.
  {fastest["lib"]} finishes in about {fastest_dur:.2f}s.</desc>
  <style>
    text {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
    .title {{ font-size: 15px; fill: #0b0b0b; font-weight: 600; }}
    .tick {{ font-size: 10px; fill: #898781; }}
    .name {{ font-size: 13px; fill: #0b0b0b; font-weight: 600; }}
    .rate {{ font-size: 10px; fill: #52514e; }}
    .badge {{ font-size: 12px; font-weight: 700; }}
    .track {{ fill: #f3f2ef; stroke: #e1e0d9; }}
    .grid {{ stroke: #e1e0d9; stroke-width: 1; }}
    .playhead {{ stroke: #0b0b0b; stroke-width: 1.5; opacity: 0.55; }}

    /* One {loop:g}s loop: the race runs 0-{race_pct:.3f}% (0-{race_seconds:g}s of
       "race time"), then every bar holds its final state for the remaining
       {_hold_seconds:g}s so the finish badges stay readable before it resets. */
    #playhead {{ animation: playhead {loop:g}s linear infinite; }}
    {styles_block}

    @keyframes playhead {{
      0% {{ transform: translateX(0); }}
      {race_pct:.3f}% {{ transform: translateX({_track_width}px); }}
      100% {{ transform: translateX({_track_width}px); }}
    }}
  </style>

  <rect width="760" height="{total_height:.0f}" fill="#fcfcfb" />
  <text x="20" y="26" class="title">HTTP client throughput race</text>

  <g>
    <line x1="{_track_x}" y1="46" x2="{playhead_x_end}" y2="46" class="grid" />
{axis_block}
    <g id="playhead">
      <line x1="{_track_x}" y1="46" x2="{_track_x}" y2="{axis_bottom}" class="playhead" />
    </g>
  </g>

{lanes_block}

  <text x="20" y="{total_height - 12:.0f}" class="rate">{footer}</text>
</svg>
"""


async def main(
    results_file: str, *, race_seconds: float = 5.0, out: str = "assets/perf-race.svg"
) -> None:
    """Regenerate the race animation SVG from a perf.py results JSON file."""
    payload = json.loads(Path(results_file).read_text())
    svg = render_svg(
        payload["results"], race_seconds, payload["total_requests"], payload["concurrency"]
    )
    Path(out).write_text(svg)
    print(f"Wrote {out} from {results_file} (race_seconds={race_seconds})")
