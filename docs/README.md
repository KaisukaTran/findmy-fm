# FINDMY – Documentation Index

Welcome! This is your guide to FINDMY documentation. Start here to understand the project, then dive into specific topics.

---

## 🎯 Start Here

**New to FINDMY?** Begin with:

1. **[Root README](../README.md)** – Project overview, quick start, features
2. **[Architecture](architecture.md)** – System design and how components interact
3. **[API Reference](api.md)** – How to use the REST API
4. **[Configuration & Secrets](configuration.md)** – Environment setup, local development, production deployment

**Contributing code?** Read:
1. **[CONTRIBUTING.md](../CONTRIBUTING.md)** – Development workflow and standards
2. **[Rules](rules.md)** – Architectural principles you must follow
3. **[Modules](modules.md)** – Code organization and module guide
4. **[Configuration](configuration.md)** – How to handle secrets safely

---

## 📁 Complete Documentation Map

| Document | Purpose | Audience |
|----------|---------|----------|
| **[configuration.md](configuration.md)** | Environment configuration, secrets management, deployment | DevOps, developers |
| **[architecture.md](architecture.md)** | System design, data flow, module responsibilities | Everyone |
| **[api.md](api.md)** | REST API endpoints, request/response examples | API users, integrators |
| **[execution.md](execution.md)** | Execution engine design, order lifecycle, limitations | Developers, traders |
| **[manual-approval.md](manual-approval.md)** | Manual order approval workflow (v0.5.0+) | Traders, developers |
| **[risk-management.md](risk-management.md)** | Pip sizing & risk checks (v0.6.0+) | Risk managers, developers |
| **[kss.md](kss.md)** | KSS Pyramid DCA strategy (v0.10.0+) | Traders, strategy developers |
| **[modules.md](modules.md)** | Code organization, class/function reference | Developers |
| **[strategy.md](strategy.md)** | How to build trading strategies, examples | Strategy developers |
| **[rules.md](rules.md)** | Architectural constraints and principles | Code reviewers, architects |
| **[roadmap.md](roadmap.md)** | Project phases, timeline, future features | Everyone |
| **[database-schema.md](database-schema.md)** | Database models and schema reference | Data engineers |
| **[SOT.md](SOT.md)** | Data model, source of truth design | Data engineers |
| **[market-integration.md](market-integration.md)** | Market data & backtesting (v0.4.0+) | Integrators, strategy devs |
| **[dashboard.md](dashboard.md)** | Dashboard UI and metrics | Traders, frontend devs |

---

## 👥 Reading by Role

### For New Developers

1. Start with [Root README](../README.md)
2. Read [Architecture](architecture.md)
3. Understand [Rules](rules.md)
4. Check [Modules](modules.md) for code organization
5. See [CONTRIBUTING.md](../CONTRIBUTING.md) before coding

### For API Users / Integrators

1. Check [API Reference](api.md)
2. Review [Excel Input Specification](../README.md#excel-input-specification)
3. See examples in [api.md](api.md#examples)
4. Run [Quick Start](../README.md#how-to-run)

### For Strategy Developers

1. Read [Strategy Guide](strategy.md)
2. Understand [Architecture](architecture.md) (especially execution flow)
3. Review [Risk Management](risk-management.md) – Pip sizing & risk checks
4. Study [Market Integration](market-integration.md) – Market data & backtesting
5. Review [Examples in Strategy Guide](strategy.md#example-strategies)
6. Test with [devlog/day-02.md](devlog/day-02.md) as reference

### For Risk Managers

1. Read [Risk Management Guide](risk-management.md) – Position limits, daily loss caps
2. Understand [Architecture](architecture.md) – How orders flow through system
3. Check [Manual Approval](manual-approval.md) – Approval workflow
4. Review [Database Schema](database-schema.md) – Risk tracking tables

### For Data Engineers

1. Read [SOT.md](SOT.md) for data model
2. Check [Database Schema](database-schema.md) for full schema
3. Review [Execution.md](execution.md#database-model)
4. Study [Modules.md](modules.md) for service details

### For Contributors

1. Read [CONTRIBUTING.md](../CONTRIBUTING.md) – Full contribution guide
2. Understand [Rules.md](rules.md) – Architectural constraints
3. Follow [DOCUMENTATION.md](../DOCUMENTATION.md) – Doc standards
4. Check [Code Standards in CONTRIBUTING.md](../CONTRIBUTING.md#code-standards)

---

## 🔄 Documentation Structure

```
docs/
├── README.md                   # This file (navigation hub)
├── architecture.md             # System design & data flow
├── api.md                      # REST API reference & examples
├── kss.md                      # KSS Pyramid DCA strategy (v0.10.0+)
├── manual-approval.md          # Order approval workflow (v0.5.0+)
├── risk-management.md          # Risk checks & pip sizing (v0.6.0+)
├── execution.md                # Execution engine design
├── manual-approval.md          # Order approval workflow (v0.5.0+)
├── risk-management.md          # Risk checks & pip sizing (v0.6.0+)
├── strategy.md                 # Strategy development guide
├── modules.md                  # Code organization & reference
├── configuration.md            # Environment & secrets config
├── database-schema.md          # Database schema reference
├── dashboard.md                # Dashboard UI & metrics
├── rules.md                    # Architectural constraints
├── roadmap.md                  # Project roadmap & phases
├── SOT.md                      # Data model reference
├── market-integration.md        # Market data & backtesting
├── advanced-execution.md       # Advanced execution features
├── devlog/                     # Development journal
│   ├── day-01.md
│   ├── day-01-EN.md
│   ├── day-02.md
│   └── day-02-EN.md
├── archive/                    # Archived documentation
├── diagrams/                   # Architecture diagrams (future)
└── plan/                       # Planning documents

/
├── README.md                   # Project overview & quick start
├── CHANGELOG.md                # Release history & changes
├── CONTRIBUTING.md             # Contribution guide & standards
├── DOCUMENTATION.md            # Documentation standards guide
└── LICENSE                     # MIT License
```

---

## 🚀 Key Concepts

### Execution Engine
A **deterministic paper trading simulator** that processes orders and tracks positions. See [execution.md](execution.md).

### Source of Truth (SOT)
The **authoritative database** storing all trading facts (orders, fills, positions). See [SOT.md](SOT.md).

### Strategy
A **stateless function** that generates trading signals from market data. See [strategy.md](strategy.md).

### API
A **FastAPI REST service** accepting Excel uploads and returning execution results. See [api.md](api.md).

---

## 📋 Development Phases

| Phase | Status | Docs |
|-------|--------|------|
| **Phase 1** – Paper Trading | ✅ Done | [roadmap.md](roadmap.md#phase-1-paper-trading-foundation-) |
| **Phase 2** – Enhanced Execution | ✅ Done | [roadmap.md](roadmap.md#phase-2-enhanced-execution-q1-2026) |
| **v0.10.0** – KSS Pyramid DCA | ✅ Done | [kss.md](kss.md) |
| **v0.9.0** – Production Readiness | ✅ Done | [roadmap.md](roadmap.md) |
| **v0.7.0** – Performance & Security | ✅ Done | [v0.7.0-release.md](v0.7.0-release.md) |
| **Phase 3** – Advanced Features | ⏳ Q2 2026 | [roadmap.md](roadmap.md) |

---

## 🔗 Quick Links

- **Project Home**: [GitHub](https://github.com/KaisukaTran/findmy-fm)
- **API Playground**: Run `./scripts/start_api.sh`, then visit `http://localhost:8000/docs`
- **Development Log**: [day-01.md](devlog/day-01.md), [day-02.md](devlog/day-02.md)
- **Report an Issue**: [GitHub Issues](https://github.com/KaisukaTran/findmy-fm/issues)

---

## ❓ Common Questions

**Q: Where do I start?**
A: Read [Root README](../README.md) then [Architecture](architecture.md).

**Q: How do I make changes?**
A: See [CONTRIBUTING.md](../CONTRIBUTING.md) for full workflow.

**Q: What are the rules I must follow?**
A: See [Rules](rules.md) for architectural constraints.

**Q: Where is the code?**
A: Core code is in `src/findmy/`. See [Modules](modules.md) for details.

**Q: How do I run the API?**
A: Run `./scripts/start_api.sh`. See [API Reference](api.md) for endpoints.

**Q: How do I write a strategy?**
A: See [Strategy Guide](strategy.md) with examples.

---

## 🔍 Searching Docs

Within VS Code:
- `Ctrl+Shift+F` (Windows/Linux) or `Cmd+Shift+F` (Mac)
- Search in `docs/` folder

Or use GitHub's search at the top of the repository.

---

## 📚 External Resources

- **Python**: [Python 3.10+ Docs](https://docs.python.org/3/)
- **FastAPI**: [FastAPI Documentation](https://fastapi.tiangolo.com/)
- **SQLAlchemy**: [SQLAlchemy Docs](https://docs.sqlalchemy.org/)
- **Pandas**: [Pandas Docs](https://pandas.pydata.org/docs/)

---

## 🤝 Contributing to Docs

Found an error or want to improve docs?

1. Fork the repository
2. Edit the `.md` file
3. Submit a pull request

See [CONTRIBUTING.md](../CONTRIBUTING.md#documentation-requirements) for guidelines.

---

## 📝 Documentation Standards

All documentation follows these standards:
- **Format**: Markdown
- **Structure**: Consistent headings and sections
- **Examples**: Always runnable code
- **Links**: All cross-references tested
- **Style**: Clear, technical, helpful tone

See [DOCUMENTATION.md](../DOCUMENTATION.md) for detailed doc standards.

---

> **Last Updated**: December 26, 2025
> 
> For changes to this index, see [docs/README.md](https://github.com/KaisukaTran/findmy-fm/blob/main/docs/README.md)
* Fill simulation
* Database schema overview
* Roadmap to live execution

---

### `strategy.md`

* Strategy interface definition
* Signal structure
* Example strategies
* Best practices (stateless, no look-ahead)

---

### `api.md`

* FastAPI endpoints
* Request / response examples
* Error handling
* Future async execution design

---

### `devlog/day-xx.md`

* Daily development journal
* Tracks:

  * What was done
  * Issues encountered
  * Fixes applied
  * Technical decisions
  * Next steps

This replaces long commit messages and preserves project memory.

---

## 📝 Template: `docs/devlog/day-02.md`

```md
# FINDMY – Development Log (Day 2)

## Objectives
-

## Work Completed
-

## Issues & Fixes
-

## Technical Decisions
-

## Lessons Learned
-

## Next Steps
-
```

---

## 📝 Template: `docs/architecture.md`

```md
# FINDMY – System Architecture

## Overview

## Core Modules
- Strategy
- Execution
- Risk
- Persistence

## Data Flow

## Design Principles

## Future Extensions
```

---

## 📝 Template: `docs/execution.md`

```md
# FINDMY – Execution Engine

## Paper Trading (v1)

## Order Lifecycle

## Database Model

## Known Limitations

## Roadmap to Live Execution
```

---

## 🔗 How This Fits with Root README

* Root `README.md` → **Project overview & quick start**
* `docs/README.md` → **Detailed technical documentation**
* `docs/devlog/` → **Project memory (Lâu Đài Ký Ức)**

---

## ✅ Recommended First Commits

```bash
git add docs/
git commit -m "docs: add documentation structure and templates"
```

---

> *Documentation is part of the system, not an afterthought.*

## Governance & Audit

FINDMY uses a canonical audit system to control architectural drift.

Latest audits:
- Day 02 – SOT Design Audit  
  - JSON: audits/runs/audit-2025-12-17T10-30.json  
  - HTML: audits/runs/audit-2025-12-17T10-30.html
