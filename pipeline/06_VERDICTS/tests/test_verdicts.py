"""Offline tests for the verdict rules.   python3 -B tests/test_verdicts.py"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import verdicts as V

R = dict(V.DEFAULT_RULES)
ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print(f"FAIL {name}: got {got!r}, want {want!r}")


def ev(t="2025-02-03", prec="day"):
    return {"event_time_utc": t, "time_precision": prec}


S2_CLEAR = {"first_after_cloud": "0.05", "clear_after_days": "2"}
S2_CLOUDY = {"first_after_cloud": "0.97", "clear_after_days": "21"}

sv = lambda e, h, **k: V.site_verdict(e, R, h, k.get("complete", True), k.get("visual", ""), k.get("flare", False),
                                      k.get("s2", S2_CLEAR), k.get("others", []))[:2]

# timing windows
check("day window, signal +20 h", sv(ev(), [20.0]), ("corroborated", "FIRMS"))
check("day window, signal -11 h", sv(ev(), [-11.0]), ("corroborated", "FIRMS"))
check("day window, signal -13 h only", sv(ev(), [-13.0]), ("not_corroborated", ""))
check("day window, signal +60 h only -> late", sv(ev(), [60.0]), ("weak", "late FIRMS"))
check("hour window, +37 h -> late", sv(ev("2025-02-03T02:00", "hour"), [37.0]), ("weak", "late FIRMS"))
check("hour window, -2 h", sv(ev("2025-02-03T02:00", "hour"), [-2.0]), ("corroborated", "FIRMS"))

# images
check("FIRMS + image", sv(ev(), [5.0], visual="smoke"), ("corroborated", "FIRMS+image"))
check("image only, no flares", sv(ev(), [], visual="new_damage_or_scar"), ("corroborated", "image"))
check("image only, flare site -> weak", sv(ev(), [], visual="active_fire", flare=True), ("weak", "image"))
check("nothing_visible is not positive", sv(ev(), [], visual="nothing_visible"), ("not_corroborated", ""))

# observability
check("cloudy, nothing", sv(ev(), [], s2=S2_CLOUDY), ("not_observable", ""))
check("cloudy but clear within 5 d -> not corroborated",
      sv(ev(), [], s2={"first_after_cloud": "0.95", "clear_after_days": "4"}), ("not_corroborated", ""))
check("cloudy, never clear", sv(ev(), [], s2={"first_after_cloud": "0.95", "clear_after_days": ""}), ("not_observable", ""))
check("no S2 info", sv(ev(), [], s2={}), ("not_corroborated", ""))
check("FIRMS incomplete", sv(ev(), [], complete=False), ("not_observable", ""))
check("signal beats cloud", sv(ev(), [3.0], s2=S2_CLOUDY), ("corroborated", "FIRMS"))

# attribution (Volgograd pattern: event 31 Jan, another attack 3 Feb, signal from +70 h... inside window here)
other = [(date(2025, 2, 4), date(2025, 2, 4), "incident 2025-02-04")]
check("signal after other incident start", sv(ev("2025-02-03"), [40.0], others=other), ("ambiguous_attribution", "FIRMS"))
check("signal before other incident", sv(ev("2025-02-03"), [5.0], others=other), ("corroborated", "FIRMS"))
check("other incident 6 days later is irrelevant",
      sv(ev("2025-02-03"), [40.0], others=[(date(2025, 2, 9), date(2025, 2, 9), "x")]), ("corroborated", "FIRMS"))
check("other incident earlier is irrelevant",
      sv(ev("2025-02-03"), [40.0], others=[(date(2025, 2, 1), date(2025, 2, 1), "x")]), ("corroborated", "FIRMS"))

check("late signal after other incident is not counted (E002 pattern)",
      sv(ev("2025-01-31"), [71.0], others=[(date(2025, 2, 3), date(2025, 2, 3), "incident 2025-02-03")]), ("not_corroborated", ""))
check("late signal, no other incident -> weak", sv(ev("2025-01-31"), [71.0]), ("weak", "late FIRMS"))
check("FIRMS + flare-site image -> FIRMS only", sv(ev(), [5.0], visual="active_fire", flare=True), ("corroborated", "FIRMS"))
check("seen-before flagged in reason",
      "seen before" in V.site_verdict(ev(), R, [], True, "smoke", False, S2_CLEAR, [], True)[2], True)

# city level
check("city: 3 overpasses vs expected 0.25", V.city_verdict(ev(), R, [1, 5, 20], 3)[0], "corroborated")
check("city: 1 overpass vs busy baseline", V.city_verdict(ev(), R, [1], 30)[0], "not_corroborated")
check("city: 0 overpasses", V.city_verdict(ev(), R, [], 0)[0], "not_corroborated")
check("city: outside window ignored", V.city_verdict(ev(), R, [70, 80], 0)[1], 0)

# statistics
check("poisson P(X>=0)", V.poisson_sf(0, 2.0), 1.0)
check("poisson P(X>=1|0.5)", round(V.poisson_sf(1, 0.5), 4), round(1 - 2.718281828 ** -0.5, 4))
check("wilson 0/10 lower", V.wilson(0, 10)[0], 0.0)
check("wilson 5/10 symmetric", round(sum(V.wilson(5, 10)), 6), 1.0)

# incidents: month-only date spans the month
inc = V.incident_dates([], [{"site_id": "S02", "date": "2024-02"}])
check("month incident", inc["S02"][0][:2], (date(2024, 2, 1), date(2024, 2, 29)))

print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
