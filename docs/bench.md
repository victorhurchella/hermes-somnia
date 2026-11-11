# Benchmark: do the principles earn their place?

`python bench/run.py --seeds 12` (oracle mode: free, deterministic, runs in CI).

## Setup

A seeded, synthetic 45-day history of a fictional indie developer (Maya: a Godot game and a small SaaS).
Every fact in it has a ground-truth label: durable facts, one-offs, trivia, a procedure, an agent-only guess, and
two facts that change over time (the SaaS moves from Fly.io to Railway; a game-jam prototype that only looks like a
stack change). The memory budget is deliberately tight at 900 chars, 58% of it already taken by entries the user
wrote by hand, so every night Somnia works under pressure.

The scripted model adds realistic noise. About 35% of its paraphrases use dangling references ("That project
uses...") and about 30% of its entries come back padded.

## Results (12 seeds, averages)

| config | precision | recall | stale | dangling | chars | skills | entries | human kept |
|---|---|---|---|---|---|---|---|---|
| all 5 on | 0.93 | 0.71 | 0.08 | 0.0 | 789 | 1 | 7.7 | 8/8 |
| **defaults** (all but inversion) | **0.94** | **0.72** | **0.08** | **0.0** | 791 | 1 | 7.7 | 8/8 |
| all off | 0.86 | 0.62 | 1.00 | 0.67 | 787 | 1 | 7.3 | 8/8 |
| without pareto | 0.90 | 0.66 | 0.67 | 0.0 | 780 | 1 | 7.3 | 8/8 |
| without lindy | 0.85 | 0.65 | 0.50 | 0.0 | 781 | 1 | 7.7 | 8/8 |
| without occam | 0.93 | 0.67 | 0.17 | 0.0 | 783 | 1 | 7.3 | 8/8 |
| without feynman | 0.97 | 0.72 | 0.25 | 0.58 | 779 | 1 | 7.8 | 8/8 |

- **precision**: share of promoted facts that are truly durable. **recall**: share of durable facts that ended in
  memory. **stale**: memory entries that were later contradicted (lower is better). **dangling**: entries that don't
  make sense on their own. **human kept**: hand-written entries still intact (Somnia never evicts them).

## Reading

- **Lindy** is the biggest win. Without it, a two-day game-jam prototype supersedes a fact confirmed over 33 days
  (precision 0.85, stale 0.50).
- **Pareto** removes stale facts. Under pressure, the rarely re-confirmed "hosted on Fly.io" entry falls into the
  tail and is retired first (stale goes from 0.08 to 0.67 without it).
- **Occam** buys recall. Padded entries waste budget, so durable facts stop fitting (recall goes from 0.72 to 0.67
  without it).
- **Feynman** removes dangling entries (0.58 per run without it). It costs a little precision, because a deferred
  paraphrase can take a night longer to come back in a clean form.
- **Inversion showed no measurable gain** here: corrections were already promoted on their own merit. Following the
  rule "what doesn't win leaves the defaults", it is **opt-in**.

Limits of the benchmark: the model is scripted, so it measures Somnia's deterministic machinery (gates, scoring,
principles, budget), not the quality of a real model's extraction. `--llm` runs the same scenario on your own model
through Hermes. It costs tokens; run it inside Hermes' Python.
