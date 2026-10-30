# Decision layer: evaluation

Date: 2026-10-29. Backend: jevos `jevos-v5` on CPU, Windows 11, Python 3.14. Reproduce with
`python tools/decision_eval.py` (needs `orchestrate.py decision setup --yes` once).

## Verdict

**Asking jevos for direct policy judgements failed (`v1`: 15/45 strict, 22/29 delegate tasks sent to `direct`).
Switching to observable task attributes + bilingual lexical cues (`v2`) fixed the routing ceiling and
under-delegation (`44/45` without runtime, `45/45` with jevos+lexical, `16/16` on held-out test, `14/14` in
Portuguese, `4/4` vague tasks abstaining).**

**Caveat: the v2 numbers are fit, not validated.** The lexical rules in `classifier.py` were written
after the v1 results and contain phrases taken from corpus tasks, test split included ("summarize its
entry points", "map the architecture", "never duplicated", "double-credit", "keeping old clients",
"80 percent", "eight", "six"). So `16/16` held out is not held out, and `44/45` measures how well the
rules memorise this corpus, not how they route new tasks. The v1 result (a real failure) and the
costs below still stand.

The zero-dependency heuristic (`source: "heuristic"`, `<1 ms`, 0 MB) is exposed in
`orchestrate.py playbook --pick --task "<task>"` as non-binding advice. Treat it as an unvalidated
hint. Keep `auto` unbuilt; judge the layer by `decision report` over real shadow runs with `mark`
outcomes, or by a fresh task set written by someone else and scored once, with no rule changes after.

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
dev (30) / test (19) split was fixed by position, before any jevos result was seen; the v2 lexical
rules were then written with the whole corpus in view (see the caveat above). They are not
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
| Route a task to a playbook before the lead plans | Unproven: strong on this corpus, which the lexical rules were tuned on; untested on new tasks |
| Pick a model tier | Same caveat; the "never cheap on non-low risk" guardrail is the useful part |
| Spot a vague task and abstain | 4 examples only; the rule is "fewer than two content words and no concrete target" |
| Portuguese tasks | Handled by the lexical cues, not by jevos (English only); 14 of 15 are translations of dev/test tasks |
| Collect evidence in shadow on real work | Works and is cheap; `decision report` compares proposals with what the lead ran and the `mark` outcomes |
| Respect provider pins, quotas, budgets | Not at risk: the layer cannot act |

## What would change the verdict

A larger labelled set from real Alethe runs (task text, playbook used, whether the result was kept
or discarded via `mark`), scored the same way, with the threshold chosen on one part and checked on
the other. Until then the corpus here is too small and too author-shaped to justify routing.

## Recommendation

Treat `playbook --pick --task "<task>"` as an unvalidated hint; its tier guardrail is the safe part.
Keep `decision shadow` with `mark` to collect real outcomes and read them with `decision report`
before enabling any automatic routing (`auto`). Do not add corpus phrases to the lexical rules; add
new tasks to a separate set instead and score them before touching the rules.
