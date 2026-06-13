# Planning-quality contract — harness-plan standard flow

`harness-plan` does not convert the information the user handed over straight into a worksheet.
When creating a plan or adding a large task, sift it through the latest information, existing specs, memory, and multi-perspective discussion via TeamAgent / subagents, and make only the elements worth adopting into the Plans.md task contract.

This is not a standalone subcommand. It is the standard quality gate for `create` and for high-impact `add`.

## Step 0: Decide whether it applies

Use this quality contract when any of the following holds.

- creating a new plan with `create`
- adding a task with `add` that affects product behavior / API / data model / permissions / billing / external integrations / distribution surface
- the user handed over an external product, competitor, spec proposal, improvement idea, or comparison material
- there is a possible conflict with existing specs, Plans.md, memory, or past decisions
- the user asked for "maximum firepower", "thorough comparison", "neutral scoring", "regression prevention", etc.
- it is not a one-off, trivial task and affects multiple tasks / multiple files / multiple sessions / product behavior / API / data model / permissions / billing / external integrations / distribution surface / security

For `create` and product-impacting `add`, read the root `spec.md` every time.
Only for a consumer repo with no root `spec.md`, fall back to an existing project spec / `docs/spec/00-project-spec.md`.
The output must always include a `Spec delta` or a `Spec skip reason`.
This is the co-required planning output contract; the precedence stays `spec.md > sub-spec > Plans.md`.

For non-trivial planning, assume TeamAgent or subagent validation.
When the Task tool is available, always run independent perspectives.
When it is not available, state `subagents not used` explicitly and evaluate the same perspectives separately on your own.
Always include `team_validation_mode` in the output.

| mode | when used |
|------|----------|
| `not_required_lightweight` | lightweight tasks such as typos / formatting / README / CHANGELOG / marker updates / status sync |
| `native` | used runtime-native multi-perspective validation such as TeamAgent |
| `subagent` | used Task subagents per perspective |
| `manual-pass` | on a runtime where Task is unavailable (e.g. OpenCode), evaluated the same perspectives separately on your own |
| `unavailable` | validation impossible. Do not mark non-trivial work as Required |

The following may be treated lightly.

- a marker-only `update`
- a status-only `sync`
- typos / formatting / README / CHANGELOG only
- a narrow change whose correct answer is fixed by existing specs and tests

## Step 1: Decompose the input

Split the information the user handed over into these four.

| Category | Example |
|------|----|
| evaluation target | external product, competitor feature, spec proposal, design approach, ops proposal |
| user's intent | what they want to improve, what they want to avoid |
| uncertain facts | recency, pricing, API, constraints, competitive landscape, existing repo state |
| evidence needed for the adoption decision | official docs, measurements, existing specs, memory, test results |

Do not stop to ask even if there are unclear points. Evaluate the reasonably-assumable intent first, and surface a "decision branch" only when the judgment is genuinely split.

## Step 2: Fetch the latest information

When external facts are involved, use WebSearch. The priority order is:

1. official documentation, official blog, release notes, GitHub repo
2. standard specs, papers, technical sources close to primary information
3. trustworthy comparison articles, case studies, issues / discussions

Confirm key facts across 2+ sources where possible.
On contradiction, organize which points contradict and make explicit the impact on the adoption decision.

When WebSearch is unavailable or the network fails, handle it as:

- `latest information: unverified`
- evaluate provisionally on local evidence only
- in the final, state explicitly "Web confirmation remains here"

## Step 3: Check the local source of truth

Any proposal to adopt into the product must be cross-checked against the existing source of truth.

What to check at minimum:

```bash
cat Plans.md
rg -n "related keywords" README.md README_ja.md CLAUDE.md docs skills scripts tests
rg -n "\"(lint|format)\"|eslint|prettier|biome|oxlint|dprint|ruff|black|isort|gofmt|go vet|cargo fmt|cargo clippy" package.json pyproject.toml go.mod Cargo.toml Makefile .github/workflows scripts docs 2>/dev/null
find docs -maxdepth 3 -type f | sort
git status --short --branch
```

Things to look at:

- whether it conflicts with the existing product promise
- whether it conflicts with existing skill role / trigger / allowed-tools
- whether it competes with incomplete tasks in Plans.md
- whether it affects the distribution mirror, Codex mirror, OpenCode mirror, or i18n
- if there is a spec source of truth, whether the spec SSOT should be updated before Plans.md
- whether the root `spec.md` product contract and the Plans.md task contract are separated
- whether a lint / formatter baseline exists for a plan with source code changes. If unset, whether a setup task is needed before implementation

## Step 4: Check memory

When harness-mem, harness-recall, or a local memory file is available, check past decisions by related keywords.
When you can search, scope it to the current project / repo. Use cross-project search only when the user explicitly asks.
This step is the wheel-reinvention check; do not skip it for non-trivial planning.

Examples of what to check:

- search results from harness-mem / harness-recall
- `.claude/agent-memory/`
- `.claude/state/memory-bridge-events.jsonl`
- existence check of `.harness-mem/`
- prior decisions left in in-repo docs / Plans.md

Notes:

- do not assume you can read the harness-mem DB directly
- if harness-mem is unset, unhealthy, or unsearchable, state "memory not checked" explicitly
- memory is weaker than the current repo state. When old memory conflicts with git / docs, prefer the current repo state
- do not conclude that what memory or search does not show is absent. `not_observed != absent`

## Step 5: Subagent discussion

For non-trivial planning, assume TeamAgent or Task subagents.
When the Task tool is available, run at least 3 independent perspectives. Tell each agent to be "read-only", "evidence-backed", "conclusion-first".
Only one-off, trivial tasks may explicitly skip this step.
Product / Strategy, Architecture / Implementation, Security / Abuse, QA / Regression, Skeptic are perspective names, not agent_type names.
Hand them as perspectives to the available TeamAgent / Task subagents.
Do not require spawning arbitrary agents.

Standard roles:

| Role | Purpose |
|------|------|
| Product / Strategy | look at adoption value, differentiation, user value, opportunity cost |
| Architecture / Implementation | look at feasibility, consistency with the existing design, maintenance load |
| Security / Abuse | look at permissions, secrets, prompt injection, supply chain, external-egress risk |
| QA / Regression | look at regressions, tests, distribution mirrors, compatibility, whether it actually works |
| Skeptic | attack the reasons not to adopt, over-investment, ambiguous premises |

What each agent's output must include:

- adopt / conditionally adopt / reject
- evidence
- the biggest risk
- what else to confirm
- conflicts with existing specs or memory
- the DoD that should land in the test / smoke / CI / review / release gate

How to summarize the discussion:

1. extract the points of agreement
2. keep the points of disagreement
3. give your own judgment
4. classify into Required / Recommended / Optional / Reject

When subagents are unavailable, evaluate the same 5 perspectives explicitly and separately on your own, and write `subagents not used`.

## Step 5.5: Implementation-plan validation gate

Do not mark an implementation plan Required until all five of the following are satisfied.

| Gate | What to check | If it fails |
|------|----------|------------|
| Spec / Plans Fit | does not conflict with the order of root `spec.md`, sub-spec, `Plans.md` | output a `Spec delta` first, or Reject |
| Memory / Wheel Check | no equivalent decision or existing task in harness-mem / harness-recall / repo memory | reuse the existing proposal, task only the delta |
| Product Fit | directly tied to the product purpose and the primary user workflow | divert to docs / external workflow / Optional |
| Security Fit | does not weaken permissions, secrets, external egress, dependencies, or branch/release gates | spike / security task / Reject |
| Quality Baseline Fit | can quality be judged Yes/No via lint / formatter / CI commands for source code changes | run a setup task first, or leave a formatter_baseline skip reason |
| Works In Practice | can it be judged Yes/No at test / smoke / CI / review / release closeout | rebuild the DoD |

This gate is "front-loaded work to reduce rework", not an opinion review.
A failed gate must be reflected in the Plans.md DoD, Depends, or `[needs-spike]`.
Quality Baseline Fit is not an excuse to sloppily add a formatter or linter.
For a plan that is unset and includes source code changes, place a setup task before the implementation tasks.
The setup task's DoD includes three points: config, package script / CI command, and validation command.
Do not install packages during planning. The introduction is done by harness-work as a setup task.
Run a broad bulk reformat only when the user explicitly asks, or when it is within that setup task's scope.
Security Fit does not require actually reading a secret.
If reading `.env`, tokens, private keys, customer data, etc. becomes necessary, stop as a Risk Gate.
Confirm via surfaces that do not read secret values, such as existing guardrails, config shape, audit evidence, tests, and GitHub / CI metadata.

## Step 6: Neutral scoring review

Score out of 5. Treat 5 as a good state and 1 as a weak state.

| Axis | 5 | 3 | 1 |
|----|-----|-----|-----|
| Product Fit | directly tied to the target product's core | useful but peripheral | another product or ops suffices |
| Evidence Strength | primary source + measurement + existing evidence | only one confirmed | mostly speculation |
| User Value | judgment quality or execution speed rises a lot | effective in some workflows | thin perceived value |
| Implementation Feasibility | small and local | mid-size but manageable | large-scale with high maintenance load |
| Regression Safety | low-risk and testable | has a blast radius | easily breaks existing flows |
| Strategic Leverage | becomes long-term differentiation | only a convenience feature | transient |
| Security Safety | verifiable without weakening permissions or secrets | has caveats | has dangerous permission relaxation or unverified external egress |
| Works In Practice | provable via smoke / CI / review | mostly manual confirmation | behavior confirmation is vague |

Correction rules:

- if Evidence Strength is 2 or below, Required is forbidden
- if Regression Safety is 2 or below, place a spike / spec / test first
- if Security Safety is 2 or below, Required is forbidden
- if Works In Practice is 2 or below, rebuild the DoD or drop it to a spike
- if Quality Baseline Fit is 2 or below and it includes source code changes, make the formatter_baseline setup task a Required dependency
- if Implementation Feasibility is 2 or below and User Value is 3 or below, lean toward Reject
- if Product Fit is 2 or below, do not put it in this product; divert it to docs / external workflow

## Step 7: `$easy` report

The final output does not present a hard evaluation as-is; it converts it into a judgeable form.

Required structure:

```markdown
In one line:
{{adoption decision in one sentence}}

Scoring review:
| Proposal | Score | Verdict | Evidence | Unverified |
|----|------|------|------|--------|

Proposals to adopt:
| Priority | Proposal | Reason | What happens |
|------|----------|------|--------------|

Regression check:
- team_validation_mode:
- spec:
- Plans.md:
- harness-mem / memory:
- TeamAgent / subagents:
- product fit:
- security:
- works in practice:
- formatter_baseline:
- mirror / distribution:
- test:

Next steps:
1. ...
2. ...
3. ...
```

Style rules:

- give the conclusion first
- translate jargon briefly right away
- do not judge by vibes like "amazing" / "innovative"
- narrow proposals to 1–3. Do not list too many candidates
- separate facts, speculation, and unverified items

## Step 8: When landing into Plans.md / spec

Convert only the adopted proposals into the task contract.

Order:

1. read the root `spec.md` and, if needed, update the product contract first as a `Spec delta`
2. if there are source code changes and the lint / formatter baseline is unset, place the formatter_baseline setup task first as a Required dependency
3. add only Required tasks to Plans.md
4. attach `[needs-spike]` to high-risk proposals
5. place a verifiable DoD on each task
6. attach `[tdd:required]` to tasks that need TDD
7. when it affects mirror / i18n / package surface, place a separate validation task
8. if no spec update is needed, leave a `Spec skip reason` in the task context / sprint contract
9. for non-trivial planning, leave the TeamAgent / subagent validation results (or the `subagents not used` fallback) and the 5-gate results in the task context
10. do not mark a `team_validation_mode: unavailable` plan as Required. Only lightweight tasks may use `not_required_lightweight`

The agent drafts the `Spec delta`. Do not assume the user writes the spec from scratch.
The `Spec delta` / `Spec skip reason` is generated by Harness; the consumer only approves or edits it.

Forbidden:

- creating only implementation tasks while the spec's correctness conditions are still shaky
- settling the regression check with a "note" instead of turning it into a task
- creating only implementation tasks while ignoring the absent lint / formatter baseline despite source code changes
- omitting the `Spec skip reason` for a docs-only / mechanical task
