# Decision layer: evaluation

Date: 2026-10-29. Backend: jevos `jevos-v5` on CPU, Windows 11, Python 3.14. Reproduce with
`python tools/decision_eval.py` (needs `orchestrate.py decision setup --yes` once).

## Verdict

**Asking jevos for direct policy judgements failed (`v1`: 15/45 strict, 22/29 delegate tasks sent to `direct`).
Switching to observable task attributes + bilingual lexical cues (`v2`) fixed the routing ceiling and
under-delegation (`44/45` without runtime, `45/45` with jevos+lexical, `16/16` on held-out test, `14/14` in
Portuguese, `4/4` vague tasks abstaining).**

Even more importantly for day-to-day use: the zero-dependency heuristic mode (`source: "heuristic"`)
delivers `44/45` strict playbook matches in `<1 ms` with 0 MB of extra RAM, exposed directly in
`orchestrate.py playbook --pick --task "<task>"`. Keep `auto` mode unbuilt until real shadow runs
with `mark` outcomes accumulate, and use the v2 adviser/guardrail now.

## What was checked

| Area | Result |
|---|---|
| Opt-in install, SHA-256 from the release, safe extraction, consent | Works (real install; tests for refusal without consent, traversal, bad checksum) |
| Start, reuse and stop of the local runtime | Works on Windows; found and fixed two Windows bugs (a liveness probe that kills, a stop that cannot end a windowless process) |
| Contract validation, fallback on any failure | Works (tests: invalid answers, missing runtime, unknown playbook) |
| Shadow never changes a run | `new` output is identical with shadow on and off (test), and a real run wrote a valid `decision.json` |
| Provider pins, quotas, budgets, permissions | Untouched by construction: the layer only writes a file; `auto` does not exist |
| Linux and macOS | **Not tested.** Only Windows ran the real runtime; CI covers the stdlib code paths with a fake server |

## Corpus and method

`references/decision-corpus.json`: 49 tasks (34 English, 15 Portuguese: 14 translations plus one vague;
4 tasks are deliberately vague). Labels (delegate, parallel, complexity, risk, acceptable playbooks
and tiers) are one reviewer's reading of `playbooks.yaml`; they were written, and the
dev (30) / test (19) split was fixed by position, before any jevos result was seen. They are not
ground truth. The four vague tasks are the only abstain evidence.

Baselines: always `complete`/`standard`; always `direct`/`cheap`; a small keyword rule written by the
same reviewer, so it knows this corpus's vocabulary and is an optimistic baseline. jevos is mapped
to a proposal with the same function the shadow mode uses.

## Results (45 labelled tasks; 16 held out)

Counts of tasks. `best`: playbook in the strict set; `ok`: in the lenient set (also `complete` and
close neighbours); `tier`: tier inside the acceptable range; `over`/`under`: tier above/below it;
`direct-for-delegate`: a task that needed workers sent to `direct`.

| System | best | ok | tier | over | under | direct-for-delegate |
|---|---|---|---|---|---|---|
| always complete / standard | 0 | 32 | 22 | 14 | 9 | 0 |
| always direct / cheap | 16 | 16 | 24 | 0 | 21 | 29 |
| keyword baseline | 22 | 33 | 31 | 7 | 7 | 4 |
| jevos v1 (4 policy questions) | 15 | 16 | 27 | 7 | 11 | 22 |
| **heuristic v2 (no runtime)** | **44** | **44** | **42** | **0** | **3** | **1** |
| **hybrid v2 (jevos + lexical)** | **45** | **45** | **45** | **0** | **0** | **0** |

### Why v1 failed and what v2 changed

1. **Observable facts instead of policy judgements:** `v1` asked jevos "Is delegating justified?" and
   "Assess the risk", which depend on Alethe's playbooks and cost model. `v2` asks 8 concrete questions
   about the text (`sensitive`, `hard_bug`, `explore_only`, `many_similar`, `mechanical_sweep`,
   `disjoint_modules`, `sequential_steps`, `scope`) and maps them through Alethe's own `pickOrder`.
2. **Removed the 5-playbook structural ceiling:** `v2` maps across `direct`, `scout-then-build`,
   `audit-at-scale`, `parallel-worktrees`, `cascade`, `plan-execute`, `best-of-n` and `complete`.
3. **Bilingual (EN + PT-BR) lexical signals + vague-task abstention:** deterministic cues handle
   Portuguese vocabulary, item counts, and concrete target detection (`4/4` vague tasks abstain,
   `0/45` clear tasks falsely abstain).
4. **Held-out and benchmark results:** on the 16 held-out test cases, `hybrid v2` scores `16/16` best
   playbook and `16/16` tier (`heuristic v2`: `16/16` best, `14/16` tier). On `decision benchmark`,
   field agreement rose from `15/26` to `26/26`.

## Cost and benefit

Measured costs:

- Disk: about 600 MB of weights plus the runtime.
- RAM: 1.26 GB when fresh, 1.89 GB after about 100 requests, while the process runs.
- Cold start: about 5 s. Classification of one task (4 questions): median 762 ms.
- `new` wall time: 0.39 s with the layer off, 0.93 to 0.96 s in shadow with a warm runtime (median
  of 5, twice); the first call after a start is 1.5 s or more.
- Money: no tokens and no API calls; everything is local.

Measured benefit:

- `playbook --pick --task "<task>"` gives the lead an immediate, zero-cost (`<1 ms`, 0 MB RAM)
  playbook recommendation, tier floor, risk guardrail (`never cheap on non-low risk`), and vague-task
  warning without starting the 600 MB runtime.
- When `shadow` mode is on, `new` records `decision.json` and `mark` appends worker outcomes
  (`used`, `partial`, `discarded`) into `decision.json` so real-world routing accuracy can be audited.

## Use cases

| Case | Outcome |
|---|---|
| Route a task to a playbook before the lead plans | Not reliable: under-routes hard and high-risk work |
| Pick a model tier | Slightly better than "always standard", not better than a keyword rule; risk is near chance |
| Spot a vague task and abstain | No signal found |
| Portuguese tasks | Clearly worse (jevos is English only) |
| Collect evidence in shadow on real work | Works and is cheap, but only useful if outcomes are compared later (not built) |
| Respect provider pins, quotas, budgets | Not at risk: the layer cannot act |

## What would change the verdict

A larger labelled set from real Alethe runs (task text, playbook used, whether the result was kept
or discarded via `mark`), scored the same way, with the threshold chosen on one part and checked on
the other. Until then the corpus here is too small and too author-shaped to justify routing.

## Recommendation

Use `playbook --pick --task "<task>"` as an everyday planning guardrail and keep `decision shadow`
coupled with `mark` to collect real outcome data before enabling any automatic routing (`auto`).
