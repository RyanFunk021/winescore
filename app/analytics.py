"""Results and awards for one tasting.

Pure functions over two DataFrames so they can be tested without a database:
  scores: member_id, member_name, wine_id, total, appearance, aroma, taste,
          aftertaste, overall, price_guess
  wines:  id, pour_no, name, producer, appellation, vintage, price, twin_of
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

CATEGORIES = {"appearance": 3, "aroma": 6, "taste": 6, "aftertaste": 3, "overall": 2}
MIN_WINES_FOR_PALATE = 3  # a rater needs this many scored wines to be judged


def _r(x, nd=1):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def wine_results(scores: pd.DataFrame, wines: pd.DataFrame) -> pd.DataFrame:
    """One row per ranked wine (secret twin copies are left out), best first."""
    ranked = wines[wines["twin_of"].isna()]
    g = scores[scores["wine_id"].isin(ranked["id"])].groupby("wine_id")
    res = pd.DataFrame({
        "mean": g["total"].mean(),
        "median": g["total"].median(),
        "sd": g["total"].std(),
        "n": g["total"].count(),
        **{c: g[c].mean() for c in CATEGORIES},
    })
    res = ranked.set_index("id").join(res, how="left")
    res = res.sort_values(["mean", "median"], ascending=False, na_position="last")
    res["rank"] = range(1, len(res) + 1)
    return res


def palate_agreement(scores: pd.DataFrame, wines: pd.DataFrame) -> pd.DataFrame:
    """Each rater's correlation with the average of everyone else (leave-one-out)."""
    ranked_ids = set(wines.loc[wines["twin_of"].isna(), "id"])
    m = (scores[scores["wine_id"].isin(ranked_ids)]
         .pivot_table(index="member_id", columns="wine_id", values="total"))
    rows = []
    for member in m.index:
        mine = m.loc[member]
        others = m.drop(index=member).mean()
        ok = mine.notna() & others.notna()
        n = int(ok.sum())
        r = np.nan
        if n >= MIN_WINES_FOR_PALATE and mine[ok].std() > 0 and others[ok].std() > 0:
            r = float(np.corrcoef(mine[ok], others[ok])[0, 1])
        rows.append({"member_id": member, "r": r, "n": n,
                     "bias": float((mine[ok] - others[ok]).mean()) if n else np.nan})
    return pd.DataFrame(rows).set_index("member_id")


def value_pick(results: pd.DataFrame):
    """Wine that beat its price by the most: residual from score ~ log(price)."""
    priced = results.dropna(subset=["price", "mean"])
    priced = priced[priced["price"] > 0]
    if len(priced) < 3:
        return None
    x = np.log(priced["price"].astype(float))
    fit = stats.linregress(x, priced["mean"].astype(float))
    resid = priced["mean"] - (fit.intercept + fit.slope * x)
    best = resid.idxmax()
    return best, float(resid[best])


def twin_pour(scores: pd.DataFrame, wines: pd.DataFrame) -> pd.DataFrame | None:
    """Per rater, the gap between their scores on a wine and its secret twin."""
    twins = wines.dropna(subset=["twin_of"])
    if twins.empty:
        return None
    t = scores.pivot_table(index="member_id", columns="wine_id", values="total")
    gaps = []
    for _, w in twins.iterrows():
        if w["id"] in t and w["twin_of"] in t:
            gaps.append((t[w["id"]] - t[w["twin_of"]]).abs())
    if not gaps:
        return None
    return pd.concat(gaps, axis=1).mean(axis=1).dropna().to_frame("gap")


def price_guessing(scores: pd.DataFrame, wines: pd.DataFrame) -> pd.Series | None:
    """Mean absolute percent error of each member's price guesses."""
    s = scores.dropna(subset=["price_guess"]).merge(
        wines[["id", "price"]], left_on="wine_id", right_on="id")
    s = s[s["price"] > 0]
    if s.empty:
        return None
    s["err"] = (s["price_guess"] - s["price"]).abs() / s["price"]
    g = s.groupby("member_id")["err"]
    return g.mean()[g.count() >= 2]


def reveal_steps(scores: pd.DataFrame, wines: pd.DataFrame, names: dict,
                 round_no: int = 1, final: bool = True) -> list[dict]:
    """The reveal for one round, in the order the host steps through it.

    Wine rankings and wine awards cover this round only. When `final` is set,
    palate awards follow, judged across every wine tasted in the event.
    """
    rw = wines[wines["round_no"] == round_no] if "round_no" in wines else wines
    rs = scores[scores["wine_id"].isin(rw["id"])] if not scores.empty else scores
    steps: list[dict] = []
    if rs.empty:
        return steps
    res = wine_results(rs, rw)

    # Wines, last place to first.
    for wid, w in res.iloc[::-1].iterrows():
        steps.append({
            "kind": "wine", "round": round_no, "rank": int(w["rank"]), "of": len(res),
            "wine_id": wid, "pour_no": int(w["pour_no"]), "name": w["name"],
            "producer": w.get("producer"), "appellation": w.get("appellation"),
            "vintage": _int(w.get("vintage")), "price": _r(w.get("price"), 2),
            "soil": w.get("soil"), "mean": _r(w["mean"]), "median": _r(w["median"]),
            "sd": _r(w["sd"]), "n": _int(w["n"]),
            "categories": {c: _r(w[c]) for c in CATEGORIES},
        })

    def award(key, title, detail, member_id=None, wine_id=None):
        steps.append({"kind": "award", "round": round_no, "key": key, "title": title,
                      "detail": detail, "member_id": member_id,
                      "member_name": names.get(member_id) if member_id else None,
                      "wine_id": wine_id})

    # Wine awards for this round (need at least two wines to mean anything).
    if len(res) >= 2:
        for cat, title in (("aroma", "Best nose"), ("aftertaste", "Best finish")):
            col = res[cat].dropna()
            if not col.empty:
                wid = col.idxmax()
                award(cat, title, f"Wine {int(res.loc[wid, 'pour_no'])}, {res.loc[wid, 'name']}: "
                      f"{col[wid]:.1f} of {CATEGORIES[cat]}", wine_id=wid)
        vp = value_pick(res)
        if vp:
            wid, lift = vp
            award("value", "Value pick", f"{res.loc[wid, 'name']} at ${res.loc[wid, 'price']:.2f} "
                  f"scored {lift:+.1f} points above what its price predicts", wine_id=wid)

    if not final:
        return steps

    # Palate awards across the whole event.
    pa = palate_agreement(scores, wines)
    judged = pa.dropna(subset=["r"])
    if len(judged) >= 3:
        top = judged["r"].idxmax()
        award("oracle", "The Oracle", f"Closest to the group (r = {judged.loc[top, 'r']:.2f})", top)
        low = judged["r"].idxmin()
        award("contrarian", "The Contrarian",
              f"Furthest from the group (r = {judged.loc[low, 'r']:.2f})", low)
    biased = pa[pa["n"] >= MIN_WINES_FOR_PALATE].dropna(subset=["bias"])
    if len(biased) >= 3:
        gen = biased["bias"].idxmax()
        award("generous", "Most generous",
              f"Scored {biased.loc[gen, 'bias']:+.1f} points above the others per wine", gen)
        tough = biased["bias"].idxmin()
        award("tough", "Toughest judge",
              f"Scored {biased.loc[tough, 'bias']:+.1f} points versus the others per wine", tough)

    tw = twin_pour(scores, wines)
    if tw is not None and len(tw) >= 2:
        twin = wines.dropna(subset=["twin_of"]).iloc[0]
        orig = wines.set_index("id").loc[twin["twin_of"]]
        best_gap = tw["gap"].min()
        tied = list(tw.index[tw["gap"] == best_gap])
        also = f" Tied with {len(tied) - 1} other{'s' if len(tied) > 2 else ''}." if len(tied) > 1 else ""
        award("twin", "Steadiest palate",
              f"Wines {int(orig['pour_no'])} and {int(twin['pour_no'])} were the same bottle. "
              f"Gap of {best_gap:.1f} points; the group averaged {tw['gap'].mean():.1f}.{also}",
              tied[0])
        steps[-1]["tied_member_names"] = [names.get(m) for m in tied]

    pg = price_guessing(scores, wines)
    if pg is not None and not pg.empty:
        best = pg.idxmin()
        award("price", "Best price guesser", f"Off by {pg[best]:.0%} on average", best)

    return steps


def member_view(scores: pd.DataFrame, member_id: str) -> dict:
    """A member's own totals by wine, shown beside the group's during the reveal."""
    mine = scores[scores["member_id"] == member_id]
    return {r.wine_id: _r(r.total) for r in mine.itertuples()}


def _int(x):
    return None if x is None or pd.isna(x) else int(x)
