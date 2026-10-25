"""Open pipeline per rep (DEMO_FLOW_TASKS D3), SIMULATED: the generator's deals are almost all closed-won, so the
reps board gets 3-6 open opportunities per rep from a seeded generator. They never enter the ledger, carry
`simulated: true`, and show a forecast commission at the plan's base rate that is not earned."""

import hashlib
import random

STAGES = ["Discovery", "Proposal", "Negotiation", "Contract sent"]
NAMES = ["Atlas", "Beacon", "Cascade", "Delta", "Evergreen", "Falcon", "Glacier", "Horizon", "Ironwood", "Juno",
         "Keystone", "Lighthouse", "Monarch", "Nimbus", "Orchid", "Pioneer"]
KINDS = ["Logistics", "Health", "Foods", "Labs", "Media", "Retail", "Systems", "Travel"]
BASE_RATE_BPS = 1000     # the standard plan's base rate: a forecast, not a calculation of earned commission


def open_deals(rep_id: str, seed: int = 7) -> list[dict]:
    rng = random.Random(int(hashlib.sha256(f"pipeline-{seed}-{rep_id}".encode()).hexdigest(), 16))
    out = []
    for n in range(rng.randint(3, 6)):
        amount = rng.randrange(20_000, 220_000, 2_500) * 100
        stage = rng.choice(STAGES)
        month = 7 + STAGES.index(stage) // 2 + rng.randint(0, 1)
        out.append({"id": f"OPP-{rep_id}-{n + 1}", "account": f"{rng.choice(NAMES)} {rng.choice(KINDS)}",
                    "stage": stage, "amount_cents": amount, "expected_close": f"2026-{month:02d}-{rng.randint(1, 28):02d}",
                    "forecast_commission_cents": amount * BASE_RATE_BPS // 10000, "earned": False, "simulated": True})
    return sorted(out, key=lambda d: (STAGES.index(d["stage"]), d["id"]))


def by_stage(deals) -> dict:
    return {stage: sum(d["amount_cents"] for d in deals if d["stage"] == stage) for stage in STAGES}
