---
trigger: always_on
glob:
description: Clean Code Refactoring Guidelines
---

# Clean Code Refactoring Guidelines

## 1. Core Philosophy

- **Craftsmanship**: Treat coding as a craft. Write code with care and pride.
- **Professionalism**: Speed comes from cleanliness. Never justify sloppy code with deadlines.
- **Readability**: Optimize for human readers. Code should read like clear prose without struggle.
- **Simplicity**: Keep designs and logic as simple as possible. Each component should do one thing well.
- **Minimalism**: Less is more. Write small, focused functions and classes; avoid gratuitous code or needless dependencies.
- **Modularity**: Organize code into cohesive, decoupled modules with explicit, minimal dependencies and clean interfaces.
- **Meaningful Naming**: Use intention-revealing names for classes, functions, and variables. Avoid cryptic abbreviations or disinformation.
- **Expressive Structure**: Reflect design intent clearly with straightforward control flow and no surprising behaviors.
- **Testability**: Design code so it can be easily unit tested. Code without tests is not clean.
- **Continuous Improvement**: Leave the code cleaner than you found it. Fix small messes at the first opportunity.

## 2. Refactoring Checklist

### Naming
- **Intention-Revealing Names**: Rename ambiguous or misleading identifiers to be self-explanatory.
- **No Disinformation or Encodings**: Avoid inaccurate type hints in names (e.g., `account_list` when it is not a list) and Hungarian notation.
- **Pronounceable & Searchable**: Avoid single-letter identifiers (except simple loop indices) and unclear abbreviations.
- **Consistent Conventions**: Use nouns for classes, verbs for functions, and consistent domain terminology.

### Functions
- **Single Responsibility**: Each function does one focused job well.
- **Small and Focused**: Keep functions short enough to grasp at once.
- **Minimal Parameters**: Prefer 0–2 parameters (rarely more than 3); group related data into objects when needed.
- **No Hidden Side Effects**: Do not unexpectedly mutate global state or input arguments.
- **One Level of Abstraction**: Avoid mixing high-level policy with low-level implementation details in the same function.

### Classes
- **Single Responsibility Principle**: Each class should have one clear responsibility and one reason to change.
- **High Cohesion & Small Size**: Keep methods and attributes tightly related to the class's core purpose.
- **Encapsulation & Minimal Coupling**: Hide internal details, expose a minimal public API, and depend only on what is required.

### Comments
- **Explain Why, Not What**: Prefer self-explanatory code; use comments to record intent, rationale, or constraints that the code itself cannot express.
- **No Obsolete Comments**: Remove commented-out dead code and outdated notes (while preserving project-mandated review/audit comments).

### Error Handling
- **Fail Fast & Fail Closed**: Validate inputs and state early; use structured exceptions rather than silent fallbacks.
- **Don't Swallow Exceptions**: Never use empty or overly broad catch blocks that hide errors.
- **Resource Cleanup**: Always release files, locks, sockets, and connections using `with` statements or `finally` blocks.
- **Meaningful Messages**: Provide clear, actionable error descriptions without leaking sensitive credentials.

### Formatting & General Cleanliness
- **Consistent Style**: Uniform indentation, logical blank-line grouping, and adherence to the project's line length (120 chars).
- **DRY & Dead Code Removal**: Eliminate duplicated knowledge and unused variables/helpers.
- **Simplify Logic**: Prefer guard clauses and straightforward control flow over deeply nested conditionals or clever one-liners.
- **Execution Flow**: Review names -> structure -> logic -> tests -> style. Refactor incrementally and confirm all relevant tests pass.
