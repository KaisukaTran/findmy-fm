# Contributing to FINDMY

Thank you for contributing to FINDMY! This guide explains how to develop, test, and submit changes.

---

## Code of Conduct

Be respectful, collaborative, and professional. We're building a trading system that people may use with real money—quality and integrity matter.

---

## Getting Started

### 1. Fork and Clone
```bash
git clone https://github.com/YOUR_USERNAME/findmy-fm.git
cd findmy-fm
```

### 2. Set Up Development Environment

**Python 3.10+** required.

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -e .  # Install in development mode

# Install dev dependencies
pip install pytest pytest-cov black flake8 mypy
```

### 3. Verify Setup
```bash
# Run tests
pytest tests/ -v

# Start API
./scripts/start_api.sh
# Should see "Uvicorn running on http://127.0.0.1:8000"
```

---

## Development Workflow

### Branch Naming

```
feat/description      - New feature
fix/description       - Bug fix
docs/description      - Documentation
refactor/description  - Code refactoring
test/description      - Tests only
chore/description     - Maintenance
```

**Examples**:
```
feat/sell-order-support
fix/position-calculation-rounding
docs/api-reference
test/execution-engine-determinism
```

### Making Changes

1. **Create feature branch**:
   ```bash
   git checkout -b feat/your-feature
   ```

2. **Write code** following the standards (below)

3. **Test changes**:
   ```bash
   # Unit tests
   pytest tests/ -v
   
   # Coverage check
   pytest tests/ --cov=findmy --cov-report=html
   
   # Code style
   black findmy/
   flake8 findmy/
   mypy findmy/ --strict
   ```

4. **Commit with clear message**:
   ```bash
   git commit -m "feat: add SELL order support
   
   - Implement reverse position logic
   - Update execution engine
   - Add tests for sell scenario
   - Update documentation"
   ```

5. **Push and open PR**:
   ```bash
   git push origin feat/your-feature
   # Open PR on GitHub
   ```

---

## Code Standards

### Python Style Guide

Follow **PEP 8** with these tools:
- `black` for formatting (10 line max, 88 char width)
- `flake8` for linting
- `mypy` for type checking

### Formatting

```bash
# Auto-format code
black findmy/ tests/

# Check style
flake8 findmy/ tests/
```

### Type Hints

All functions should have type hints:

```python
# Good
def execute_orders(
    orders: List[Order],
    portfolio: PortfolioState
) -> ExecutionResult:
    """Execute orders and return result."""
    ...

# Bad
def execute_orders(orders, portfolio):
    """Execute orders and return result."""
    ...
```

### Docstrings

Use **Google-style docstrings**:

```python
def calculate_position(fills: List[Fill]) -> Position:
    """
    Calculate aggregate position from fills.
    
    Args:
        fills: List of fill records.
        
    Returns:
        Position object with size, avg_cost, last_updated.
        
    Raises:
        ValueError: If fills is empty or invalid.
        
    Example:
        >>> fills = [Fill(qty=0.5, price=65000)]
        >>> pos = calculate_position(fills)
        >>> assert pos.size == 0.5
    """
```

### Naming Conventions

```python
# Classes: PascalCase
class ExecutionEngine:
    pass

# Functions: snake_case
def execute_orders():
    pass

# Constants: UPPER_SNAKE_CASE
MAX_POSITION_SIZE = 10.0

# Private: _leading_underscore
def _calculate_internal_state():
    pass

# Avoid: reserved words, unclear names
# Bad: e, data, process_thing
# Good: execution_result, market_data, validate_order
```

### Import Organization

```python
# 1. Standard library
import json
from datetime import datetime
from typing import List

# 2. Third-party
import pandas as pd
from sqlalchemy import Column, String

# 3. Local
from findmy.api.schemas import ExecutionResponse
from findmy.execution.paper_execution import Order
```

---

## Testing Requirements

### Unit Tests

All new functions must have tests:

```bash
pytest tests/ -v
```

**Example test**:
```python
import pytest
from findmy.execution.paper_execution import Order, execute_orders

def test_execute_single_buy_order():
    """Execute single BUY order."""
    orders = [Order(symbol="BTC/USDT", qty=0.5, price=65000)]
    result = execute_orders(orders)
    
    assert len(result.fills) == 1
    assert result.fills[0].qty == 0.5
    assert result.positions["BTC/USDT"].size == 0.5

def test_execute_invalid_qty():
    """Reject orders with invalid quantity."""
    orders = [Order(symbol="BTC/USDT", qty=-0.5, price=65000)]
    
    with pytest.raises(ValueError):
        execute_orders(orders)
```

### Coverage Target

- New code: **≥ 80% coverage**
- Existing code: Maintain or improve
- Critical paths (execution, SOT): **≥ 90%**

```bash
pytest tests/ --cov=findmy --cov-report=term-missing --cov-fail-under=80
```

### Integration Tests

Test end-to-end workflows (Excel → API → Execution → SOT):

```python
def test_paper_execution_e2e(tmp_path):
    """Test full pipeline: upload → execute → persist."""
    # Create Excel file
    excel_file = create_test_excel(tmp_path)
    
    # Upload via API
    response = client.post("/paper-execution", files={"file": excel_file})
    
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "success"
    
    # Verify persisted in SOT
    orders = sot.query_orders()
    assert len(orders) == 5
```

---

## Architectural Constraints

**Must be satisfied before merge**:

1. **Separation of Concerns**: Each module has one responsibility
2. **Determinism**: No `random`, `now()`, external API calls in core logic
3. **Immutability**: Historical facts never modified (only appended)
4. **No Cross-Layer Coupling**: Lower layers don't depend on upper layers
5. **Explicit Errors**: Custom exceptions, meaningful messages
6. **Auditability**: All decisions recorded in append-only log

See [rules.md](docs/rules.md) for detailed enforcement.

---

## Documentation Requirements

### Code Comments

```python
# Bad: States the obvious
price = order.price  # Set price to order.price

# Good: Explains why
price = order.price  # Use order price, not market price (to ensure determinism)
```

### Function Documentation

All public functions must have:
- Docstring (purpose, args, return, raises)
- Type hints
- Usage example (for complex functions)

### Module Documentation

Each module should have a clear `__init__.py` or README:

```python
# findmy/execution/__init__.py
"""
Execution engine for trading orders.

This module provides deterministic order execution:
- Paper trading (simulated fills)
- Position tracking
- No state retention (stateless function)

Example:
    from findmy.execution import execute_orders
    fills = execute_orders(orders)
"""

from .paper_execution import execute_orders, Order

__all__ = ["execute_orders", "Order"]
```

### Documentation Changes

If PR adds/changes features, also update:
- [ ] Relevant `.md` file in `docs/`
- [ ] Docstrings in code
- [ ] Examples in documentation
- [ ] API reference if endpoints change
- [ ] Roadmap if scope changes

---

## PR Checklist

Before submitting a pull request, verify:

- [ ] **Code Quality**
  - [ ] `black` formatted
  - [ ] `flake8` no errors
  - [ ] `mypy` type checking passes
  - [ ] No unused imports

- [ ] **Testing**
  - [ ] All tests pass (`pytest tests/`)
  - [ ] New tests for new code (≥80% coverage)
  - [ ] No flaky tests
  - [ ] Integration tests pass

- [ ] **Architecture**
  - [ ] Follows separation of concerns
  - [ ] No determinism violations
  - [ ] No cross-layer coupling
  - [ ] Explicit error handling
  - [ ] Audit trail complete

- [ ] **Documentation**
  - [ ] Docstrings complete
  - [ ] Type hints present
  - [ ] `.md` files updated
  - [ ] Examples provided
  - [ ] No broken links

- [ ] **Git**
  - [ ] Commit messages clear
  - [ ] Branch name follows convention
  - [ ] No merge conflicts
  - [ ] Rebased on latest main

---

## Review Process

### What Reviewers Check

1. **Code Quality**: Style, clarity, maintainability
2. **Correctness**: Tests pass, logic sound
3. **Architecture**: Rules followed, design appropriate
4. **Performance**: No regressions, scalability considered
5. **Documentation**: Clear and complete

### Feedback Response

- **Mandatory changes**: Must be addressed (marked "required")
- **Optional suggestions**: Consider for improvement
- **Questions**: Clarify intent

**Request re-review** after addressing feedback:
```
Addressed feedback: 
- Changed X to Y
- Added tests for Z
- Updated documentation

Ready for re-review.
```

---

## Commit Message Format

```
<type>: <subject>

<body>

<footer>
```

### Type
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `refactor`: Code reorganization
- `test`: Tests only
- `chore`: Build, deps, tooling

### Subject
- Imperative mood ("add" not "added")
- Don't capitalize first letter
- No period at end
- < 50 characters

### Body (Optional)
- Explain *why*, not *what*
- Wrap at 72 characters
- Reference issues: "Fixes #123"

### Example
```
feat: add SELL order support

- Implement reverse position logic
- Update execution engine to handle SELL
- Add position closing and realized P&L
- Update SOT schema with exit_price

Fixes #45
Related to #42
```

---

## Debugging Tips

### Run Tests with Output
```bash
pytest tests/test_execution.py -v -s  # -s shows print statements
```

### Run Single Test
```bash
pytest tests/test_execution.py::test_buy_order -v
```

### Debug with PDB
```python
import pdb; pdb.set_trace()  # Breakpoint

# In debugger:
# (Pdb) l          # list code
# (Pdb) n          # next line
# (Pdb) p var      # print variable
# (Pdb) c          # continue
```

### Check Database State
```python
from services.sot import repository

orders = repository.query_all_orders()
for order in orders:
    print(f"{order.id}: {order.symbol} {order.qty}")
```

### Profile Performance
```bash
python -m cProfile -s cumulative scripts/profile_execution.py
```

---

## Requesting Help

- **Questions?** Open a GitHub issue with "question" label
- **Need guidance?** Comment on PR during review
- **Found bug?** Create issue with "bug" label + reproduction steps
- **Want feature?** Start a discussion or create feature request

---

## Large Changes / RFCs

For large architectural changes or RFCs:

1. **Open discussion issue** first
2. **Create RFC document** in `docs/rfc/`
3. **Get feedback** from maintainers
4. **Implement** after approval
5. **Update docs** to reflect decision

Example RFC file: `docs/rfc/0001-add-sell-orders.md`

---

## Merging Requirements

PR can only merge if:

- ✅ All tests pass
- ✅ Code review approved (≥1 maintainer)
- ✅ No conflicts with main
- ✅ All CI checks green
- ✅ Documentation updated

---

## After Merge

- Your code is now in production (or will be soon)
- Monitor for issues
- Be available for questions
- Help with code reviews of others

---

## Questions?

- Check [docs/README.md](docs/README.md) for documentation index
- See [rules.md](docs/rules.md) for architecture rules
- Open issue for help

**Thank you for contributing!** 🚀
