# sync subcommand — progress-sync flow

Reconcile the implementation status with Plans.md, detect diffs, and update.

## Step 0: Validate Plans.md

Check that Plans.md exists and its format. If there is a problem, guide immediately and stop.

| State | Guidance |
|------|------|
| Plans.md does not exist | `Plans.md not found. Create it with /harness-plan create.` → **stop** |
| header has no DoD / Depends columns (v1 format) | `Plans.md is the old format (3 columns). Regenerate to v2 (5 columns) with /harness-plan create. Existing tasks carry over automatically.` → **stop** |
| v2 format (5 columns) | proceed to Step 1 as-is |

## Step 1: Collect the current state (parallel)

```bash
# Plans.md state
cat Plans.md

# Git change state
git status
git diff --stat HEAD~3

# Recent commit history
git log --oneline -10

# Agent trace (recently edited files)
tail -20 .claude/state/agent-trace.jsonl 2>/dev/null | jq -r '.files[].path' | sort -u
```

## Step 1.5: Agent Trace analysis

Get the recent edit history from the Agent Trace and reconcile it with the Plans.md tasks:

```bash
# list of recently edited files
RECENT_FILES=$(tail -20 .claude/state/agent-trace.jsonl 2>/dev/null | \
  jq -r '.files[].path' | sort -u)

# project info
PROJECT=$(tail -1 .claude/state/agent-trace.jsonl 2>/dev/null | \
  jq -r '.metadata.project')
```

**Reconciliation points**:

| Check item | Detection method |
|------------|----------|
| file edits not in Plans.md | Agent Trace vs task descriptions |
| files different from the task description | expected files vs actual edits |
| tasks with no long-running edits | Agent Trace timeline vs WIP duration |

## Step 2: Detect diffs

| Check item | Detection method |
|------------|----------|
| done but `cc:WIP` | commit history vs marker |
| started but `cc:TODO` | changed files vs marker |
| `cc:done` but uncommitted | git status vs marker |

### Artifact Hash backward compatibility

Recognize both the `cc:done [a1b2c3d]` form (with commit hash) and `cc:done` (no hash).

**Matching rules**:
- `cc:done` → treat as done without a hash
- `cc:done [xxxxxxx]` → treat as done with a hash. Preserve the 7-char short hash
- with a hash, you can reconcile against `git log --oneline` to confirm the commit exists

> **backward compatible**: the no-hash form is still valid. Do not break existing Plans.md.

## Step 3: Propose Plans.md updates

When diffs are detected, propose and then execute:

```
Plans.md needs updating

| Task | Current | After | Reason |
|------|------|--------|------|
| XX   | cc:WIP | cc:done | committed |
| YY   | cc:TODO | cc:WIP | files edited |

Update? (yes / no)
```

## Step 4: Output the progress summary

```markdown
## Progress summary

**Project**: {{project_name}}

| Status | Count |
|----------|------|
| not started (cc:TODO) | {{count}} |
| in progress (cc:WIP) | {{count}} |
| done (cc:done) | {{count}} |
| PM reviewed (pm:reviewed) | {{count}} |

**Progress**: {{percent}}%

### Recently edited files (Agent Trace)
- {{file1}}
- {{file2}}
```

## Step 5: Propose the next action

```
Next steps

**Priority 1**: {{task}}
- Reason: {{requested / waiting to unblock}}

**Recommended**: harness-work, harness-review
```

## Anomaly detection

| Situation | Warning |
|------|------|
| multiple `cc:WIP` | multiple tasks in progress at once |
| `pm:requested` unprocessed | process the PM's request first |
| large divergence | task management is not keeping up |
| WIP not updated for 3+ days | check whether it is blocked |

## Step 6: Retrospective (default ON)

When running `sync`, if there is at least one `cc:done` task, automatically run a retrospective.
Can be explicitly skipped with `--no-retro`.

### Step R1: Collect completed tasks

```bash
# extract cc:done / pm:reviewed tasks from Plans.md
grep -E 'cc:done|pm:reviewed' Plans.md

# recent completion-commit history
git log --oneline --since="7 days ago"

# change size
git diff --stat HEAD~10
```

### Step R2: Retrospective — 4 items

| Item | Analysis method |
|------|---------|
| **estimation accuracy** | infer the expected file count from the Plans.md task descriptions → compare with the actual changed-file count from `git diff --stat` |
| **blocker causes** | tally the reason patterns of tasks marked `blocked` (technical / external dependency / spec unclear) |
| **quality-marker hit rate** | whether tasks tagged `[feature:security]` etc. actually had related issues |
| **scope change** | task count at the first Plans.md commit vs the current task count (additions/deletions) |

### Step R3: Output the retrospective summary

```markdown
## Retrospective summary

**Period**: {{start_date}} – {{end_date}}

| Metric | Value |
|------|-----|
| completed tasks | {{count}} |
| blockers occurred | {{blocked_count}} |
| scope change | +{{added}} / -{{removed}} |
| estimation accuracy | expected {{est}} files → actual {{actual}} files |

### Learnings
- {{1-2 lines of learning}}

### To apply next
- {{1-2 lines of improvement action}}
```

### Step R4: Record to harness-mem

Record the retrospective result to harness-mem so it can be referenced on the next `create`.
Destination: the relevant agent memory under `.claude/agent-memory/`.
