"""Rate-card fare baseline, a sample of tracts to collect real quotes for, and the fare regression model."""

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

from src.config import PROCESSED_DIR

# per-trip TNC fee each airport charges on drop-offs, passed through to the rider (FY 2026/27 schedules)
AIRPORT_FEES = {"SFO": 6.00, "OAK": 3.70, "SJC": 4.00}

# Lyft standard rate cards. Silicon Valley is from lyft.com/pricing/SJC; San Francisco is a third-party
# figure (getridewise.com, Nov 2026) because lyft.com/pricing/SFO is offline. Upfront prices drift from these.
RATE_CARDS = {
    "silicon_valley": {"base": 2.58, "per_mile": 0.84, "per_minute": 0.36, "service_fee": 3.60, "minimum": 3.59},
    "san_francisco": {"base": 2.00, "per_mile": 1.50, "per_minute": 0.28, "service_fee": 2.50, "minimum": 7.25},
}
SANTA_CLARA = "085"


def rate_card_fare(routes):
    """Fare implied by the published rate card plus airport fee and tolls, before surge and upfront-pricing adjustments."""
    routes = routes.copy()
    region = np.where(routes["GEOID"].str[2:5] == SANTA_CLARA, "silicon_valley", "san_francisco")
    card = pd.DataFrame([RATE_CARDS[r] for r in region], index=routes.index)
    ride = card["base"] + card["per_mile"] * routes["miles"] + card["per_minute"] * routes["minutes"]
    routes["ride_charge"] = np.maximum(ride, card["minimum"])
    routes["service_fee"] = card["service_fee"]
    routes["airport_fee"] = routes["code"].map(AIRPORT_FEES)
    routes["rate_card_fare"] = routes[["ride_charge", "service_fee", "airport_fee", "toll"]].sum(axis=1)
    return routes


def quote_sample(routes, workers, centroids, per_airport=40, seed=0):
    """Pick tracts to collect app quotes for: spread evenly across distance, drawn from tracts where airport workers live."""
    df = routes.merge(workers[["code", "GEOID", "S000"]], on=["code", "GEOID"])
    centroids = centroids.to_crs(4326)
    df = df.merge(pd.DataFrame({"GEOID": centroids["GEOID"], "lat": centroids.geometry.y.round(5), "lon": centroids.geometry.x.round(5)}), on="GEOID")
    picks = []
    for code, group in df.groupby("code"):
        # equal draws from each distance decile so long trips aren't crowded out by the dense urban core
        group = group.assign(decile=pd.qcut(group["miles"], 10, labels=False))
        picks.append(group.groupby("decile").sample(per_airport // 10, weights=group["S000"], random_state=seed))
    sample = pd.concat(picks).drop(columns="decile").sort_values(["code", "miles"])
    sample["maps_link"] = "https://www.google.com/maps/dir/" + sample["lat"].astype(str) + "," + sample["lon"].astype(str) + "/" + sample["code"] + "+airport"
    for column in ["service", "requested_at", "price", "quoted_minutes", "notes"]:
        sample[column] = ""
    return sample


def fit_fare_model(quotes, formula="net_price ~ miles + C(code)"):
    """OLS of the quoted price net of known pass-throughs (airport fee, tolls), with robust standard errors.

    Pass-throughs are subtracted rather than estimated because a few dozen quotes can't pin down
    a toll coefficient that is known to be exactly 1. Free-flow minutes correlate 0.99 with miles,
    so the default leaves them out; add the app's congestion-aware quoted_minutes instead.
    """
    quotes = quotes.dropna(subset=["price"]).copy()
    quotes["net_price"] = quotes["price"] - quotes["code"].map(AIRPORT_FEES) - quotes["toll"]
    model = smf.ols(formula, data=quotes).fit(cov_type="HC3")
    return model


def predict_fares(model, routes):
    routes = routes.copy()
    routes["predicted_fare"] = model.predict(routes) + routes["code"].map(AIRPORT_FEES) + routes["toll"]
    routes.to_csv(f"{PROCESSED_DIR}/tract_airport_fares.csv", index=False)
    return routes
