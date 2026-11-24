"""Summarize completed field trials without assigning survey credit."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from scipy.stats import beta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    parser.add_argument("--score", type=float, required=True, help="observed candidate score")
    parser.add_argument(
        "--fields", type=int, required=True, help="all fields tested in this survey"
    )
    args = parser.parse_args()
    if args.fields < 1 or not math.isfinite(args.score):
        parser.error("A finite score and positive survey size are required")

    def read(p):
        return json.loads(p.read_text())

    protocol = read(args.study / "protocol.json")
    bins = defaultdict(list)
    nulls = []
    for path in sorted(args.study.glob("*-*.json")):
        row = read(path)
        if row.get("protocol") != protocol:
            raise ValueError("Trial protocol differs")
        if "maximum" in row:
            nulls.append(row["maximum"])
        for case in row.get("injections", []):
            mag = round(20.44 - 2.5 * math.log10(case["flux"]), 3)
            bins[mag].append(case["assessed"] and case["recovered"] is True)
    recovery = []
    for mag, trials in sorted(bins.items()):
        recovery.append(
            dict(
                T=mag,
                V_for_V_minus_T={str(c): mag + c for c in (-0.5, 0, 0.5)},
                recovered=sum(trials),
                attempted=len(trials),
            )
        )
    summary = dict(recovery=recovery, successful_null_trials=len(nulls))
    if nulls:
        n = len(nulls)
        k = sum(value is not None and value >= args.score for value in nulls)
        upper = beta.ppf(0.95, k + 1, n - k) if k < n else 1.0
        summary.update(
            null_exceedances=k,
            monte_carlo_p=(1 + k) / (1 + n),
            survey_bonferroni_p=min(1.0, args.fields * (1 + k) / (1 + n)),
            survey_upper_95=min(1.0, args.fields * upper),
        )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
