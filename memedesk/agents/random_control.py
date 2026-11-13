"""Random: the luck baseline. It skips the Analyst entirely.

Each cycle, if the Analyst passes N coins, Random draws N coins uniformly from
the same cycle's filter survivors (the Analyst's own pool, including coins the
Analyst rejected) and sends them to its own book. Same executor, same sizing,
same risk exits. If the scored book doesn't beat this book over enough trades,
the score isn't adding edge over the filter alone.
"""
from __future__ import annotations

import random
import time

from ..models import RandomPick, Vetted, require_stage


class RandomControl:
    name = "random"
    book = "random"

    def __init__(self, seed: int = 0):
        self.seed = seed or int(time.time())
        self.rng = random.Random(self.seed)

    def pick(self, pool: list[Vetted], n: int) -> list[RandomPick]:
        for v in pool:
            require_stage(v, "filter", self.name)
        if n <= 0 or not pool:
            return []
        n = min(n, len(pool))
        chosen = self.rng.sample(range(len(pool)), n)
        return [RandomPick(vetted=pool[i], index=i, pool_size=len(pool),
                           trail=pool[i].trail + [self.name]) for i in chosen]
