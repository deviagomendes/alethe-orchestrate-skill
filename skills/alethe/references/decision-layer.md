# Decision layer (optional, off by default)

A small local classifier that reads a task and **proposes** a playbook, tier and runner strategy.
It never decides: provider pins, quotas, budgets, permissions, isolation and the playbooks stay
exactly as they are. Without it, Alethe works as before, with no network and no Python extras.

## Modes

| Mode | Asks the classifier | Changes a run |
|---|---|---|
| `off` (default) | no | no |
| `shadow` | on `new`, if the runtime is installed | no: it only writes `decision.json` in the orchestration folder |

`auto` does not exist yet. Any failure (no runtime, timeout, bad answer, low confidence) leaves the
native flow untouched.

## Commands

```
orchestrate.py playbook --pick --task "task text"   # fast non-binding advice (works without jevos)
orchestrate.py decision status
orchestrate.py decision setup            # shows what would be downloaded; nothing happens yet
orchestrate.py decision setup --yes      # only after the user agreed
orchestrate.py decision setup --endpoint http://127.0.0.1:8017   # use a runtime you already run
orchestrate.py decision shadow | off
orchestrate.py decision classify "task text" [--wait 60]
orchestrate.py decision benchmark [--fixtures file.json]
orchestrate.py decision stop             # only a runtime Alethe started
orchestrate.py decision report           # shadow proposals vs. what the lead ran and the mark outcomes
```

## Install and privacy

- Backend: [jevos](https://github.com/feder-cr/jev) (MIT), release `jevos-v5`, a native CPU binary
  plus about 600 MB of weights, about 1-1.4 GB of RAM while running. Not the proprietary Jev.
- Downloaded only after consent, over HTTPS, checked against the release's `SHA256SUMS.txt`,
  extracted without links or paths that leave the folder, into the user's Alethe data folder
  (never a project).
- The runtime listens on `127.0.0.1` only and the client ignores proxies: the task text stays on
  the machine. `decision.json` holds the proposal, never the task text.
- `decision off` stops asking; delete the `decision` folder next to the policy file to remove it.

## What the proposal means

Instead of asking jevos to make policy judgements directly, `scripts/decision/classifier.py` asks
eight observable questions (`sensitive`, `hard_bug`, `explore_only`, `many_similar`,
`mechanical_sweep`, `disjoint_modules`, `sequential_steps`, `scope`) and combines them with
bilingual (EN + PT-BR) lexical cues. When the jevos runtime is off or not installed, `playbook --pick --task`
uses the zero-dependency lexical heuristic immediately (`source: "heuristic"`).

The mapping covers Alethe's full `pickOrder` (`direct`, `scout-then-build`, `audit-at-scale`,
`parallel-worktrees`, `cascade`, `plan-execute`, `best-of-n`, `complete`), limited to ids that exist
in the active `playbooks.yaml` and strategies. Risky work never maps to the `cheap` tier, and vague
requests without a concrete target abstain (`abstained: true`).

When a worker is later marked with `orchestrate.py mark <worker> <used|partial|discarded>`, any
`decision.json` in that orchestration folder is updated with `outcomes` and `outcomeSummary` so
shadow proposals can be evaluated against real outcomes.

**Status: advisory / shadow.** On the 49-task corpus (45 labelled + 4 vague) and a 20-task unseen
stress set, switching from 4 policy questions (15/45 strict, 22/29 under-routed to `direct`) to
observable questions + bilingual lexical signals reached 44/45 strict in heuristic mode and 45/45 in
hybrid mode, with 0/29 delegate-worthy tasks sent to `direct` and 4/4 vague tasks abstaining.
These v2 numbers are fit to the corpus: the lexical rules were written with it in view and quote some
of its tasks, so they say little about new tasks. Numbers and method: `docs/decision-layer-evaluation.md`; reproduce with
`python tools/decision_eval.py`.

## Measuring

`decision benchmark` runs `references/decision-benchmark.json` and reports latency (the first row
includes the cold start), errors and how many fields match the fixture labels. The labels are a
reviewer's judgement, not ground truth, and agreement with them says nothing about whether a
proposal would have cost less. Compare `decision.json` against what the lead actually chose
before drawing conclusions.

`decision report` does that comparison over every orchestration folder that has a `decision.json`:
how often the proposal matched the lead's playbook, and the `used`/`partial`/`discarded` worker
marks split by whether they agreed. Many discards where the lead overrode the proposal are evidence
for the layer; many discards where they agreed are evidence against it.

## Still off by default for auto-routing

`auto` mode remains unbuilt until shadow runs with `mark` outcomes confirm savings on real Alethe work.
