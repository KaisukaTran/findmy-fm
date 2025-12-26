# FINDMY – Documentation Standards

This document defines how documentation is written, organized, and maintained in FINDMY.

---

## Purpose of Documentation

Documentation serves multiple purposes:
- **Onboarding**: Help new developers understand the system
- **Reference**: Quick lookup of APIs, modules, patterns
- **Architecture**: Explain design decisions and rationale
- **Maintenance**: Preserve institutional knowledge
- **Audit**: Record project decisions and evolution

---

## Documentation Structure

```
docs/
├── README.md              # Documentation index & entry point
├── architecture.md        # System design & data flow
├── api.md                 # API reference & endpoints
├── execution.md           # Execution engine details
├── modules.md             # Module reference & code organization
├── strategy.md            # Strategy interface & examples
├── roadmap.md             # Project phases & timeline
├── rules.md               # Architectural rules & constraints
├── SOT.md                 # Source of Truth data model
├── devlog/                # Development journal
│   ├── day-01.md
│   ├── day-02.md
│   └── day-xx.md
└── diagrams/              # Architecture diagrams
    ├── architecture.png
    ├── data-flow.png
    └── module-dependencies.png

/
├── README.md              # Project overview & quick start
├── CONTRIBUTING.md        # How to contribute
└── DOCUMENTATION.md       # This file (docs standards)
```

---

## Document Types & Guidelines

### 1. README Files

**Purpose**: Entry point, quick orientation, navigation.

**Location**: 
- Root `README.md`: Project overview
- `docs/README.md`: Documentation index
- Module `docs/modules/X/README.md` (future)

**Content**:
- What is this?
- Why does it exist?
- Links to detailed docs
- Quick start / hello world

**Example Structure**:
```markdown
# FINDMY (FM) – High-Level Description

## What Is FINDMY?
[1-2 sentences]

## Key Features
- Feature 1
- Feature 2

## Quick Start
[3-5 commands]

## Next Steps
- See [architecture.md](docs/architecture.md) for design details
- See [api.md](docs/api.md) for API reference
```

### 2. Reference Documentation

**Purpose**: Detailed specifications, APIs, data models.

**Files**: `api.md`, `modules.md`, `execution.md`

**Content**:
- Comprehensive specification
- All endpoints/functions listed
- Request/response formats
- Error codes and handling
- Examples for each item
- Related docs cross-references

**Format**:
```markdown
# Title

## Overview
[Purpose and scope]

## Sections
### Section 1
- Point 1
- Point 2

### Section 2
[Table, code, example]
```

### 3. Architecture Documentation

**Purpose**: Explain design, decisions, tradeoffs.

**Files**: `architecture.md`, `rules.md`

**Content**:
- System overview
- Design principles
- Data flow diagrams
- Module interactions
- Design decisions + rationale
- Future extensions

**Must include**:
- "Why" not just "what"
- Diagrams where helpful
- Examples of correct/incorrect patterns
- References to rules

### 4. Strategy Documentation

**Purpose**: Teach strategy development, patterns, examples.

**File**: `strategy.md`

**Content**:
- Strategy interface definition
- Design principles (stateless, no look-ahead)
- Code examples (simple → complex)
- Common pitfalls
- Testing patterns
- Integration with execution

**Examples should be**:
- Runnable code
- Clearly documented
- Progressive (basic → advanced)

### 5. Development Logs

**Purpose**: Record progress, decisions, learnings.

**Files**: `devlog/day-XX.md`

**Content**:
- Date and author
- Objectives for the day
- Work completed
- Issues encountered & fixes
- Technical decisions + rationale
- Lessons learned
- Next steps

**Format**:
```markdown
# FINDMY – Development Log (Day XX)

**Date**: [Date]
**Author**: [Name]
**Timeline**: [Start - End]

## Objectives
- Objective 1
- Objective 2

## Work Completed
1. Item 1: [details]
2. Item 2: [details]

## Issues & Fixes
### Issue 1: [Problem description]
- Root cause: ...
- Solution: ...

## Technical Decisions
- Decision 1: [Choice], because [reasoning]
- Decision 2: [Choice], because [reasoning]

## Lessons Learned
- Learning 1
- Learning 2

## Next Steps
- Task 1
- Task 2

## Time Log
- 9:00-10:30 — Work on feature X
- 10:30-11:00 — Code review
```

---

## Writing Standards

### Style & Tone

**Use**:
- Active voice: "The engine executes orders" (not "Orders are executed")
- Second person for instructions: "You create a file" (not "One creates")
- Present tense: "The API provides" (not "will provide")
- Imperative for steps: "Create file", "Run tests"

**Avoid**:
- Jargon without explanation
- Assumptions about reader knowledge
- Vague language ("might", "probably", "should")
- Humor (keep it professional)

**Tone**: Clear, technical, helpful. Like talking to a colleague.

### Headings

```markdown
# Top-Level Heading (Document Title)
## Main Section
### Subsection
#### Sub-subsection (rarely needed)
```

**Rules**:
- Use `#` for document title (only one)
- Use `##` for major sections
- Use `###` for subsections
- Use `####` only if necessary
- All caps section names are acceptable (e.g., "## API ENDPOINTS")

### Code Examples

**Python**:
```python
# Good: Runnable, minimal, clear
def calculate_position(fills):
    total_qty = sum(f.qty for f in fills)
    avg_cost = sum(f.qty * f.price for f in fills) / total_qty
    return Position(size=total_qty, avg_cost=avg_cost)
```

**Shell**:
```bash
# Good: Clear, single task
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@orders.xlsx" | jq '.positions'
```

**JSON**:
```json
{
  "status": "success",
  "positions": [
    {"symbol": "BTC/USDT", "size": 0.5}
  ]
}
```

**Rules**:
- Always show the language (` ```python `)
- Make examples complete and runnable
- Use real values, not `<placeholder>`
- Add comments explaining non-obvious parts
- Keep examples minimal (don't show 50 lines)

### Lists & Tables

**Unordered lists**:
```markdown
- Item 1
- Item 2
  - Nested item
  - Another nested
- Item 3
```

**Ordered lists** (for procedures):
```markdown
1. Step one
2. Step two
   - Sub-step (use letter/dash for clarity)
3. Step three
```

**Tables** (for comparisons):
```markdown
| Feature | Status | Notes |
|---------|--------|-------|
| Buy Orders | ✅ Done | v1 |
| Sell Orders | ⏳ Planned | v2 |
```

### Callouts

```markdown
> **Note**: This is important information

> **Warning**: This could cause problems

> **Tip**: This is helpful advice
```

### Cross-References

**Link to other docs**:
```markdown
See [architecture.md](architecture.md) for system design.

See [modules.md](modules.md#execution-module) for execution details.
```

**Link to sections**:
```markdown
See [Design Principles](#design-principles) below.

See [architecture.md#design-principles](architecture.md#design-principles).
```

**Don't use**:
- Absolute URLs to files
- Non-existent links (test them!)
- `click here` as link text

**Do use**:
- Relative paths from current file
- Descriptive link text
- Markdown links `[text](url)`

---

## Maintaining Documentation

### Synchronization with Code

**Rule**: Documentation should match code within 24 hours of change.

**When code changes**:
1. Update relevant `.md` files
2. Update docstrings in code
3. Update examples
4. Update related files (architecture, modules, API)

**When docs are wrong**:
1. File issue or PR
2. Don't let it sit

### Version Control

**Docs are code**: Commit to git like code.

```bash
git add docs/*.md
git commit -m "docs: update API reference for new endpoints"
```

**PR requirements for doc changes**:
- Clear subject line
- Explain what changed and why
- Link to related code PR if applicable

### Review Before Merge

Docs should be reviewed like code:
- Accuracy: Does it match implementation?
- Clarity: Is it understandable?
- Completeness: Are all cases covered?
- Examples: Do they work?
- Links: Are they correct?

---

## Documentation Patterns

### Explaining a Module

```markdown
# Module Name

## Overview
[1-2 sentences: what it does]

## Responsibility
[What it's responsible for]

## Design
[Key design patterns used]

## Interface
[Public functions/classes]
- `function1()`: Description
- `function2()`: Description

## Example Usage
[Simple, runnable example]

## Testing
[How to test it]

## References
[Links to related docs]
```

### Explaining a Feature

```markdown
## Feature Name

### Purpose
[Why this feature exists]

### How It Works
[Sequence of events, with flow diagram if helpful]

### Usage
[How to use it]

### Example
[Real example with expected output]

### Limitations
[Known limits or future improvements]
```

### Explaining an Error

```markdown
### Error: ERROR_CODE

**Cause**: [What causes this error]

**Example**:
```
Error message or stack trace
```

**Solution**:
1. Check that ...
2. Verify that ...
3. Try ...
```

---

## Tools & Validation

### Markdown Linting

Use a markdown linter to catch issues:
```bash
# Check markdown
markdownlint docs/*.md

# Auto-fix common issues
npx prettier --write docs/*.md
```

### Link Checking

Verify links are correct:
```bash
# Check all links
find docs -name "*.md" -exec grep -H "](http" {} \;

# Test markdown links manually
# (VS Code: Cmd+Click on link)
```

### Spell Checking

```bash
# Check spelling
aspell -c docs/*.md
```

### Document Generation

**Future**: Generate HTML docs from markdown
```bash
# Generate from markdown
mkdocs build -f mkdocs.yml
```

---

## Quality Checklist

Before submitting doc changes:

- [ ] **Accuracy**
  - [ ] Matches current code
  - [ ] Examples are correct
  - [ ] All APIs documented

- [ ] **Clarity**
  - [ ] No jargon without explanation
  - [ ] Short sentences
  - [ ] Clear structure

- [ ] **Completeness**
  - [ ] All sections present
  - [ ] No broken links
  - [ ] Cross-references working

- [ ] **Format**
  - [ ] Markdown valid
  - [ ] Headings consistent
  - [ ] Code blocks highlighted

- [ ] **Examples**
  - [ ] Runnable and correct
  - [ ] Well-documented
  - [ ] Real values (not placeholders)

---

## Common Mistakes

| ❌ Mistake | ✅ Solution |
|-----------|-----------|
| Docs don't match code | Update docs when code changes |
| Broken links | Test links before merging |
| Vague explanations | Explain the "why", not just "what" |
| Giant examples | Keep examples minimal, link to real code |
| Outdated information | Archive old docs, don't delete |
| No structure | Use consistent headings & sections |
| Spelling errors | Run spell check |

---

## Future Improvements

- [ ] HTML site generation (mkdocs)
- [ ] API docs from code (Swagger/OpenAPI)
- [ ] Automatic link validation
- [ ] Documentation versioning
- [ ] Glossary of terms
- [ ] Video tutorials

---

## Questions & Feedback

- Have a doc question? Check the docs first
- Found an issue? File an issue with "docs" label
- Want to improve? Open a PR with improvements
- Need clarification? Comment on PR

---

## References

- [Docs README](docs/README.md) – Documentation index
- [Architecture](docs/architecture.md) – System design
- [Contributing Guide](CONTRIBUTING.md) – How to contribute

---

> *Good documentation is an investment in the future. Write for the person who will maintain this code in 2 years.*
