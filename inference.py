from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp

from legacy_routesum import LegacyRouteSum
from large_csv import LargeCSVProfile


@dataclass
class RuleInference:
    rules: pd.DataFrame = field(default_factory=pd.DataFrame)
    route_fit: pd.DataFrame = field(default_factory=pd.DataFrame)
    confidence: int = 0
    exact_fit: bool = False
    source: str = ""
    warnings: List[str] = field(default_factory=list)


def _category_counts(routesum: LegacyRouteSum, legacy_tx: Optional[LargeCSVProfile]) -> tuple[pd.DataFrame, str]:
    frames = []
    sources = []
    have_keys = not routesum.key_counts.empty
    have_ttps = not routesum.ttp_counts.empty
    if have_keys:
        frames.append(routesum.key_counts[["route", "identifier", "count"]])
        sources.append("ROUTESUM keys")
    if have_ttps:
        frames.append(routesum.ttp_counts[["route", "identifier", "count"]])
        sources.append("ROUTESUM TTPs")

    # Fill only missing category families from Transaction Detail. This avoids replacing a
    # clean ROUTESUM matrix with lower-confidence transaction-derived counts.
    if legacy_tx is not None and not legacy_tx.route_identifier.empty and (not have_keys or not have_ttps):
        tx = legacy_tx.route_identifier[["route", "identifier", "event_count"]].rename(columns={"event_count": "count"})
        if not have_keys:
            k = tx[tx["identifier"].str.startswith("KEY ", na=False)]
            if not k.empty:
                frames.append(k); sources.append("Transaction Detail keys")
        if not have_ttps:
            t = tx[tx["identifier"].str.startswith("TTP ", na=False)]
            if not t.empty:
                frames.append(t); sources.append("Transaction Detail TTPs")

    if frames:
        counts = pd.concat(frames, ignore_index=True).groupby(["route", "identifier"], as_index=False)["count"].sum()
        return counts, " + ".join(sources)
    return pd.DataFrame(columns=["route", "identifier", "count"]), "Unavailable"


def infer_legacy_ridership_rules(routesum: LegacyRouteSum, legacy_tx: Optional[LargeCSVProfile] = None) -> RuleInference:
    if routesum.route_summary.empty:
        return RuleInference(warnings=["Legacy route ridership totals are unavailable; rules cannot be inferred."])
    counts, source = _category_counts(routesum, legacy_tx)
    if counts.empty:
        return RuleInference(source=source, warnings=["No Legacy key/TTP category counts were available for automatic ridership-rule inference."])

    route_df = routesum.route_summary.copy()
    route_df["route"] = route_df["route"].astype(str)
    counts["route"] = counts["route"].astype(str)
    totals = counts.groupby("identifier")["count"].sum()
    categories = sorted(totals[totals > 0].index.tolist())
    if not categories:
        return RuleInference(source=source, warnings=["No nonzero Legacy key/TTP categories were available for rule inference."])

    routes = route_df["route"].tolist()
    rindex = {r: i for i, r in enumerate(routes)}
    cindex = {c: j for j, c in enumerate(categories)}
    A = np.zeros((len(routes), len(categories)), dtype=float)
    for _, row in counts.iterrows():
        r, c = str(row["route"]), row["identifier"]
        if r in rindex and c in cindex:
            A[rindex[r], cindex[c]] += float(row["count"])

    preset = route_df.get("preset", pd.Series(np.zeros(len(route_df)))).astype(float).to_numpy()
    actual = route_df["ridership"].astype(float).to_numpy()
    target = actual - preset

    explicit = {}
    if not routesum.key_display_ridership.empty:
        explicit = routesum.key_display_ridership.groupby("identifier")["displayed_ridership"].sum().to_dict()

    n, m = len(categories), len(routes)
    # Binary rule variables + positive/negative route fit errors.
    # Route fit dominates; prior terms only break ties between exact solutions.
    objective = np.zeros(n + 2 * m)
    prior_labels = []
    for j, cat in enumerate(categories):
        if cat.startswith("TTP "):
            objective[j] = -1.0  # weak prior: pass/TTP events generally board unless totals say otherwise
            prior_labels.append("weak rider prior")
        elif explicit.get(cat, 0) > 0:
            objective[j] = -3.0  # detailed Legacy ridership table explicitly shows it as a rider
            prior_labels.append("Legacy detailed rider display")
        else:
            objective[j] = 0.20  # weak non-rider prior; route totals can override (e.g. COLTS Key 8 / D)
            prior_labels.append("weak non-rider prior")
    objective[n:] = 10000.0

    Aeq = np.c_[A, np.eye(m), -np.eye(m)]
    constraint = LinearConstraint(Aeq, target, target)
    integrality = np.r_[np.ones(n), np.zeros(2 * m)]
    bounds = Bounds(np.zeros(n + 2 * m), np.r_[np.ones(n), np.full(2 * m, np.inf)])

    result = milp(
        c=objective,
        integrality=integrality,
        bounds=bounds,
        constraints=constraint,
        options={"time_limit": 12},
    )
    if result.x is None:
        return RuleInference(source=source, warnings=[f"Rule inference optimizer failed: {result.message}"])

    flags = np.rint(result.x[:n]).astype(int)
    predicted = preset + A.dot(flags)
    residual = actual - predicted
    abs_error = float(np.abs(residual).sum())
    exact = abs_error < 1e-6
    total_riders = max(float(actual.sum()), 1.0)
    error_rate = abs_error / total_riders
    confidence = 99 if exact and "Transaction Detail" not in source else (90 if exact else max(45, int(90 - error_rate * 1000)))
    if "Transaction Detail" in source:
        confidence = min(confidence, 82)

    rule_rows = []
    display_totals = routesum.key_display_ridership.groupby("identifier")["displayed_ridership"].sum().to_dict() if not routesum.key_display_ridership.empty else {}
    for j, cat in enumerate(categories):
        count = float(totals.get(cat, 0))
        inferred = bool(flags[j])
        displayed = float(display_totals.get(cat, 0)) if cat.startswith("KEY ") else np.nan
        contribution = count if inferred else 0.0
        internal_mismatch = bool(cat.startswith("KEY ") and not np.isnan(displayed) and abs(displayed - contribution) > 1e-9)
        rule_rows.append({
            "identifier": cat,
            "legacy_event_count": count,
            "inferred_counts_ridership": inferred,
            "inferred_legacy_ridership": contribution,
            "legacy_displayed_key_ridership": displayed,
            "legacy_display_mismatch": internal_mismatch,
            "prior_basis": prior_labels[j],
        })
    rules = pd.DataFrame(rule_rows)
    fit = route_df[["route", "ridership", "preset"]].copy()
    fit["inferred_from_categories"] = predicted
    fit["residual"] = residual

    warnings = []
    if not exact:
        warnings.append(
            f"Automatic rules could not perfectly reconstruct Legacy ridership; absolute residual is {abs_error:g} riders. "
            "Rule-related conclusions are lower confidence."
        )
    return RuleInference(rules=rules, route_fit=fit, confidence=confidence, exact_fit=exact, source=source, warnings=warnings)
