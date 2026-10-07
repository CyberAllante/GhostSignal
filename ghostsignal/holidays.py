"""Holiday calendar for sourcing: what's coming, when to buy, when to ship, and what to look for.

Seasonal products sell in a burst before the date and then die. The money is in having stock listed before the
ramp, so every holiday carries suggested deadlines:
  - FBA: inventory should be *at Amazon* before demand ramps. Q4 check-in gets slow, so those deadlines are earlier.
  - FBM: you ship yourself, so you can keep buying until about a week out.
These are planning suggestions, not Amazon's official cutoffs (Amazon publishes its own holiday inbound dates).
"""

from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


# name -> (date rule, ramp_days before the date when buyers start shopping, FBA send-by days before, keywords)
HOLIDAYS = {
    "Super Bowl":       (lambda y: _nth_weekday(y, 2, 6, 2), 21, 30, ["football party supplies", "game day snacks", "football decorations"]),
    "Valentine's Day":  (lambda y: date(y, 2, 14), 28, 35, ["valentines day gifts", "valentines candy", "valentines day cards kids", "heart shaped chocolate"]),
    "St. Patrick's Day": (lambda y: date(y, 3, 17), 21, 30, ["st patricks day decorations", "st patricks day shirt"]),
    "Easter":           (lambda y: _easter(y), 35, 42, ["easter basket stuffers", "plastic easter eggs", "easter candy", "easter basket"]),
    "Mother's Day":     (lambda y: _nth_weekday(y, 5, 6, 2), 28, 35, ["mothers day gifts", "gifts for mom", "mom journal"]),
    "Graduation":       (lambda y: date(y, 5, 25), 35, 40, ["graduation gifts", "graduation party decorations"]),
    "Father's Day":     (lambda y: _nth_weekday(y, 6, 6, 3), 28, 35, ["fathers day gifts", "gifts for dad", "grill tools gift set"]),
    "4th of July":      (lambda y: date(y, 7, 4), 21, 30, ["4th of july decorations", "american flag decorations", "patriotic party supplies"]),
    "Back to School":   (lambda y: date(y, 8, 15), 35, 45, ["school supplies", "kids backpack", "crayola back to school", "composition notebook"]),
    "Halloween":        (lambda y: date(y, 10, 31), 42, 45, ["halloween decorations", "halloween costume kids", "halloween candy", "halloween party favors"]),
    "Thanksgiving":     (lambda y: _nth_weekday(y, 11, 3, 4), 28, 40, ["thanksgiving decorations", "turkey roasting pan", "thanksgiving table decor", "fall harvest decor"]),
    "Black Friday":     (lambda y: _nth_weekday(y, 11, 3, 4) + timedelta(days=1), 21, 45, ["toys", "kitchen gadgets gift", "board games", "lego sets"]),
    "Christmas":        (lambda y: date(y, 12, 25), 56, 50, ["christmas ornaments", "stocking stuffers", "advent calendar", "christmas decorations",
                                                             "christmas gifts for kids", "ugly christmas sweater", "christmas lights", "gift sets"]),
    "New Year's":       (lambda y: date(y + 1, 1, 1) - timedelta(days=0), 14, 21, ["new years eve party supplies", "2027 planner", "new years decorations"]),
}
FBM_BUY_BY_DAYS = 7      # ship-it-yourself: keep buying until about a week out


def upcoming(today: date | None = None, count: int = 8) -> list[dict]:
    today = today or date.today()
    rows = []
    for name, (rule, ramp, fba_lead, keywords) in HOLIDAYS.items():
        for year in (today.year, today.year + 1):
            day = rule(year)
            if day >= today:
                break
        fba_by, fbm_by, ramp_start = day - timedelta(days=fba_lead), day - timedelta(days=FBM_BUY_BY_DAYS), day - timedelta(days=ramp)
        days_left = (day - today).days
        if today <= fba_by:
            status, advice = ("prep" if (fba_by - today).days > 21 else "now"), f"Send FBA stock by {fba_by:%b %-d}"
        elif today <= fbm_by:
            status, advice = "fbm", f"Too late for FBA. Ship it yourself; buy by {fbm_by:%b %-d}"
        else:
            status, advice = "late", "Too late this year. Clearance after the holiday can stock next year cheaply."
        rows.append({"name": name, "date": day.isoformat(), "days_left": days_left, "ramp_start": ramp_start.isoformat(),
                     "fba_send_by": fba_by.isoformat(), "fbm_buy_by": fbm_by.isoformat(), "status": status,
                     "advice": advice, "keywords": keywords, "origin": f"holiday:{name}"})
    return sorted(rows, key=lambda r: r["days_left"])[:count]


def keywords_for(name: str) -> list[str]:
    return HOLIDAYS[name][3] if name in HOLIDAYS else []
