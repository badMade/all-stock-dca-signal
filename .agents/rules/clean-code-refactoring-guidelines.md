---
trigger: always_on
glob:
description: # Clean Code Refactoring Guidelines

## Part 1: Clean Code Philosophy and Practices Prompt

- **Craftsmanship**: Treat coding as a craft. Write code with care and pride – clean code looks like it was written by someone who *cares* about the craft[[1]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=I%20could%20list%20all%20of,cares%20deeply%20about%20the%20craft)[[2]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Michael%20hit%20it%20on%20the,They%20have%20cared). Practice continually and hone your “code-sense” through discipline and hard work.
- **Professionalism**: Responsibility for code quality lies with the developer. Never justify sloppy code with deadlines – *the only way to go fast is to keep the code clean*[[3]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=True%20professionals%20know%20that%20the,as%20possible%20at%20all%20times). A true professional writes maintainable, well-structured code and defends its quality.
- **Readability**: Optimize for human readers. Clean code is easy to read and understand, often reading like well-written prose[[4]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Clean%20code%20is%20simple%20and,and%20straightforward%20lines%20of%20control). Another developer (or your future self) should be able to grasp the code’s intent without struggle.
- **Simplicity**: Keep designs and logic as simple as possible. Each component should have a straightforward purpose – **clean code does one thing well**[[5]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=I%20like%20my%20code%20to,code%20does%20one%20thing%20well). Avoid unnecessary complexity; solutions should be clear, obvious, and minimal.
- **Minimalism**: Less is more. Write small, focused functions and classes, and avoid gratuitous code or needless dependencies. Smaller codebases are easier to maintain (smaller is better[[6]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Dave%20uses%20the%20word%20minimal,Smaller%20is%20better)), so eliminate redundancies and don’t add features that aren’t required.
- **Modularity**: Organize code into cohesive, decoupled modules. Each module or class should have a clear responsibility and explicit, minimal dependencies[[7]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Clean%20code%20can%20be%20read%2C,not%20all%20necessary%20information%20can). Provide one obvious way to do each task, and design clean interfaces.
- **Meaningful Naming**: Use intention-revealing names for all identifiers. Classes, functions, and variables should be named to clearly communicate their purpose[[8]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Expressiveness%20to%20me%20includes%20meaningful,saying%20how%20it%20is%20done). Avoid cryptic abbreviations or misleading terms – good names make comments unnecessary.
- **Expressive Structure**: Code should clearly reflect the design intent. Each function, class, or module has a single, focused purpose[[9]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Bjarne%20closes%20with%20the%20assertion,unpolluted%2C%20by%20the%20surrounding%20details), and the code’s structure exposes the logical flow transparently (no convoluted control paths). Clean code has *crisp abstractions* and straightforward control flow, with no surprising behaviors.
- **Testability**: Clean code is test-driven and **fully tested** – if it has no tests, it isn’t clean[[10]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Dave%20ties%20cleanliness%20to%20tests%21,not%20tests%2C%20it%20be%20unclean). Design code so that it can be easily unit tested (e.g. avoid hidden dependencies or tight coupling). Tests act as a safety net and living documentation, ensuring the code works as intended and enabling fearless refactoring.
- **Continuous Improvement**: Continuously refactor and improve the code. Apply the Boy Scout Rule: *leave the code cleaner than you found it*[[11]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Leave%20the%20campground%20cleaner%20than,you%20found%20it). Don’t let “broken windows” (small messes) accumulate[[12]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Pragmatic%20Dave%20Thomas%20and%20Andy,starts%20the%20process%20toward%20decay) – clean up issues at the first opportunity. Constant refinement and **continuous improvement** are intrinsic to professional software development[[13]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Can%20you%20imagine%20working%20on,an%20intrinsic%20part%20of%20professionalism).

*(Use the above principles as guidelines for writing or refactoring code. Instruct any AI or team member to uphold these values for every code change.)*

## Part 2: Game Plan and Checklist for Clean Code Refactoring

Follow this step-by-step checklist to refactor and clean up code. For each category, review the code and address each point:

### Naming

- **Intention-Revealing Names** – Does every class, function, and variable name clearly convey its intent and purpose? Rename any ambiguous or misleading names to be self-explanatory.
- **No Disinformation or Encodings** – Ensure names are accurate (e.g. don’t use “list” in a name for something that isn’t a list) and free of unnecessary encodings or Hungarian notation.
- **Pronounceable & Searchable** – Are names easy to say and search for? Avoid one-letter identifiers (except simple loop indices) and unclear abbreviations. Use clear, descriptive terms so that someone can find occurrences easily.
- **Consistent Conventions** – Follow consistent naming conventions. For example, use nouns for classes, verbs for functions, and meaningful domain terminology where appropriate. Ensure similar things have similar names and different things don’t accidentally share names.

### Functions

- **Single Responsibility** – Does each function **do one thing** and do it well (one focused behavior)[[9]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Bjarne%20closes%20with%20the%20assertion,unpolluted%2C%20by%20the%20surrounding%20details)? If a function is doing multiple tasks, split it into smaller functions each handling a single concern.
- **Small and Focused** – Keep functions short and focused. Can the whole function be viewed at once? Long functions should be refactored into smaller ones for clarity and reuse.
- **Descriptive Names** – Verify that each function’s name describes exactly what it does. The implementation should match the name’s expectation (no surprises or hidden behaviors).
- **Minimal Parameters** – Do functions have a small number of parameters (ideally 0-2, and rarely more than 3)? If a function has many parameters or flag arguments, consider refactoring by grouping data into objects or splitting the function.
- **No Side Effects** – Check that functions don’t have hidden side effects. A function should not unexpectedly modify global state or its arguments. If side effects are needed, make them obvious in the name and documentation, otherwise refactor to eliminate them.
- **One Level of Abstraction** – Within a function, the statements should all be at a similar level of abstraction. Avoid mixing high-level policy with low-level details in the same function; instead, delegate or extract code so that each function operates uniformly at one abstraction layer.

### Classes

- **Single Responsibility Principle** – Does each class represent a single concept or responsibility? A class should have only one reason to change. If a class is handling multiple unrelated duties, break it into focused classes.
- **Cohesion** – Are all the methods and variables of a class related to its core purpose? Remove or refactor any elements that don’t directly contribute to the class’s primary responsibility. High cohesion makes classes easier to understand and maintain.
- **Small Classes** – Keep classes small and focused, similar to small functions. Avoid god classes that try to do everything. A class should be sized such that it can be described in a concise sentence.
- **Clear Naming** – Ensure each class name describes its intent or role in the system. The name should reflect what the class represents. If you can’t describe the class’s purpose easily, it might be doing too much.
- **Encapsulation** – Verify that classes hide their internal implementation details. Use private/internal members appropriately and expose a clear, minimal API. Other parts of the code should interact with a class only through its intended public interface.
- **Minimal Coupling** – Check that classes depend only on what they need. Imports or dependencies should be minimal and explicit[[7]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Clean%20code%20can%20be%20read%2C,not%20all%20necessary%20information%20can). Remove any unused dependencies. Ensure that changing one class has minimal impact on others (low coupling).

### Comments

- **Necessary Only** – Use comments sparingly and only for things that are not obvious from the code itself. If the code is clear, it shouldn’t need explanatory comments. Write a comment only to explain **why** something is done in a certain way, not **what** the code is doing.
- **Explain Intent & Context** – When comments are present, ensure they provide context or intent that the code cannot. For example, record the rationale behind a complex algorithm or a tricky decision. Avoid restating the code in words.
- **No Redundant/Obsolete Comments** – Remove any comments that are outdated or no longer relevant to the current code. Delete commented-out code; version control has the history if needed. Old TODO/FIXME notes should be resolved or turned into formal tasks, not left to rot in the code.
- **Prefer Self-Explanatory Code** – If you find yourself wanting to write a comment to explain what a section of code does, consider refactoring the code to be more self-expressive instead. For example, use well-named functions or variables to make the code readable without a comment. (Clean code rarely needs many comments.)

### Error Handling

- **Use Exceptions (or Equivalents) for Errors** – Ensure that error conditions are handled by throwing exceptions, returning error objects, or other accepted patterns, rather than subtle error codes or ignoring errors. Error handling logic should be clear and separated from normal logic.
- **Complete and Clear** – Is every error case accounted for? Review the code for any failure paths (null returns, catches, etc.) to verify that nothing is ignored. **Error handling should be complete** and thoughtful – no silent catches or missing checks[[5]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=I%20like%20my%20code%20to,code%20does%20one%20thing%20well).
- **Fail Fast** – Make the code fail fast on bad data or unexpected conditions. Validate inputs and state early, then proceed with the assurance that everything is valid. This prevents cascading errors and simplifies error handling.
- **Don’t Swallow Exceptions** – If using try/catch, do not catch general exceptions without handling them or rethrowing. Each catch should either handle the problem or propagate it appropriately; avoid empty catch blocks.
- **Resource Cleanup** – Verify that resources (files, database connections, network sockets, etc.) are properly closed or released in error cases. Use finally blocks or defer statements to ensure cleanup happens even if an exception occurs. No resource leaks should be present after refactoring.
- **Meaningful Messages** – Ensure that error messages or exception details clearly describe the problem and, if helpful, how to address it. This makes debugging easier and is part of writing clean, communicative code.

### Formatting

- **Consistent Style** – Apply a consistent coding style throughout. Ensure indentation is correct and uniform, braces/brackets are aligned, and there’s a consistent use of spaces around operators and after commas. The code should look uniform as if written by a single knowledgeable developer.
- **Logical Structure** – Use formatting to reveal structure. Separate code into logical paragraphs using blank lines to group related statements. For example, leave a blank line between different blocks of logic or before a new section within a function.
- **Line Length & Wrapping** – Keep line lengths reasonable (e.g., under ~80-120 characters) to avoid horizontal scrolling and improve readability. If a statement is too long or complex, consider refactoring or breaking it into multiple lines/steps for clarity.
- **Vertical Ordering** – Organize code in a file in a sensible order (e.g., high-level components or public methods first, detail implementations later, or grouped by functionality). Readers should be able to navigate the file and find things easily.
- **No Trailing Mess** – Remove any trailing whitespace, fix misaligned indentations, and ensure the file ends with a newline. These little details (while not affecting runtime) reflect cleanliness and attention to detail.

### Testing

- **Adequate Test Coverage** – Ensure the code is covered by **automated tests** (unit tests, and integration tests if applicable). Every function or module of significance should have corresponding tests verifying its behavior. Remember: *code without tests is not clean*[[10]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Dave%20ties%20cleanliness%20to%20tests%21,not%20tests%2C%20it%20be%20unclean).
- **All Tests Passing** – Run the test suite after refactoring. All tests must pass to confirm that changes didn’t break functionality. If tests fail, adjust the code (or tests, if the code behavior was supposed to change) until you have green tests. Maintain the discipline of keeping tests green.
- **Write Tests for New Changes** – If you had to add new functionality or found untested cases during refactoring, write new tests for them. Do not introduce code that isn’t verified by tests. Consider writing tests first (TDD style) for any bug fixes or new features you implement during the cleanup.
- **Testability of Design** – Inspect if the refactored code is easier to test. For example, are functions free of side effects and external dependencies? Can you instantiate classes and pass in mock dependencies easily? If some code is hard to test, consider refactoring its design (inject dependencies, break apart large functions, etc.) to improve testability.

### General (Overall Cleanliness)

- **DRY (Don’t Repeat Yourself)** – Eliminate duplicate code. If the same or similar code appears in multiple places, refactor to unify it (extract a function or use a common module). **Clean code contains no duplication**[[14]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=Runs%20all%20the%20tests%3B) – each piece of knowledge in the system should have a single, unambiguous representation[[15]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=No%20duplication%2C%20one%20thing%2C%20expressiveness%2C,Everything%20is%20there).
- **Remove Dead Code** – Delete any code that is not being used: unused variables, unnecessary functions, leftover commented-out blocks, etc. If it’s not needed, it doesn’t belong in the codebase. This reduces noise and potential confusion.
- **Simplify Logic** – Look for overly complex logic, deeply nested loops/conditionals, or ad-hoc solutions. Refactor complex constructs into simpler, clearer ones (e.g., break down large conditionals with guard clauses, use polymorphism or strategy to replace big switch/case statements). The goal is **straightforward logic** that’s easy to follow[[5]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=I%20like%20my%20code%20to,code%20does%20one%20thing%20well).
- **Avoid Premature Optimization** – Ensure that the code is not littered with complicated optimizations that sacrifice clarity. Clear and simple code is preferred; optimize only when there is evidence of a performance problem, and even then, do it in a clean and measured way.
- **Consistent Architecture** – Verify that the refactored code aligns with the overall architecture and conventions of the project. For example, if the project has a certain layering or pattern (MVC, etc.), make sure the code fits into that structure without introducing special cases or hacks.
- **Self-Review** – Finally, do a last read-through of the code. Does each routine “turn out to be pretty much what you expected” when you read it[[16]](https://www.notion.so/Clean-code-269202e5e677802d829ae01d59841ccf?showMoveTo=true&saveParent=true#:~:text=You%20know%20you%20are%20working,was%20made%20for%20the%20problem)? If you were a new developer on the project, would this code be easy to understand and modify? Address any lingering issues. The code should *feel* clean – simple, readable, and single-purpose – after applying all the above steps.

Use this checklist as a guide when refactoring code with the help of an LLM or manually. Tackle one category at a time, ensuring each aspect of the code meets these clean code standards. By following these steps, the code will become more maintainable, elegant, and professional in quality, reflecting the core philosophy of clean code craftsmanship.

## Version 1

Here’s a **token-efficient rewrite** of your *Clean Code Refactoring Guidelines* prompt — keeping all philosophical and actionable content, but compressing phrasing, cutting redundancy, and removing link overhead (those URLs can stay referenced elsewhere in docs or Notion).

### 🧭 Clean Code Refactoring Guidelines (Lean Agent Version)

### 1. Core Philosophy

- **Craftsmanship**: Code like you care. Treat it as craft, not churn.
- **Professionalism**: Speed comes from cleanliness. No excuses for sloppy code.
- **Readability**: Code should read like prose — intent clear without comments.
- **Simplicity**: Do one thing well. Fewer moving parts, fewer mistakes.
- **Minimalism**: Less code, fewer deps, smaller surface = easier maintenance.
- **Modularity**: Cohesive, decoupled pieces with clear interfaces.
- **Naming**: Use names that reveal purpose. Avoid abbreviations or deception.
- **Expressiveness**: Code mirrors design intent; no surprise logic.
- **Testability**: If it’s untestable, it’s unclean. Write code that can be tested.
- **Continuous Improvement**: Always leave code cleaner than you found it.

### 2. Refactoring Checklist

**Naming**

- Use intention-revealing, consistent, searchable names.
- No disinformation, no Hungarian, no one-letter vars.
- Match nouns for classes, verbs for functions.

**Functions**

- One focused job; short enough to grasp at once.
- Descriptive names; minimal params (<3 ideally).
- No hidden side-effects. Keep one abstraction level per function.

**Classes**

- One clear responsibility.
- High cohesion; small and descriptive.
- Encapsulate internals; expose minimal API.
- Keep coupling low and explicit.

**Comments**

- Use only to explain *why*, not *what*.
- Remove obsolete comments or TODOs.
- Prefer expressive code over explanations.

**Error Handling**

- Use structured exceptions, not silent failures.
- Fail fast on bad input.
- Always clean up resources (finally/defer).
- Make messages meaningful for debugging.

**Formatting**

- Uniform indentation and spacing; consistent style.
- Logical grouping with blank lines.
- Reasonable line length (~100 chars).
- Keep files tidy and ordered.

**Testing**

- Ensure strong coverage; every important path tested.
- All tests green before merge.
- Write new tests for new code or fixes.
- Refactor for easier testability (inject deps, pure funcs).

**General Cleanliness**

- DRY: eliminate duplication.
- Remove dead or unused code.
- Simplify complex logic — prefer clarity over cleverness.
- Avoid premature optimization.
- Follow architectural patterns; don’t introduce hacks.
- Final review: does the code *read clean* and *behave predictably*?

### 3. Directive

Apply these principles and checklist systematically when refactoring or writing code. Evaluate every change through clarity, simplicity, and maintainability. Clean code should be *obvious, minimal, and self-explanatory*.

Would you like me to compress this further into a **runtime-ready format** (single-string JSON or YAML block under 600 tokens) for direct agent embedding?

## Version 2

Here’s a **lean “agent-ready” version** of your *Clean Code Refactoring Guidelines* — distilled for embedding in an instruction or system prompt while keeping the full role intent and workflow logic. It’s under 600 tokens and ready for downstream compression.

### 🧠 Agent Prompt — Clean Code Refactoring Guidelines

You are an expert software craftsman.Your task: refactor or write code to uphold **Clean Code** principles—clarity, simplicity, testability, and professionalism.Treat code quality as a core deliverable, not an afterthought.

**Philosophy**Write code that looks intentional. Clean code is simple, readable, minimal, modular, and testable.Leave every file cleaner than you found it. Optimize for humans, not machines.

**Refactoring Checklist**

1. **Naming** – Names reveal intent; stay consistent and searchable. Avoid abbreviations, disinformation, or one-letter vars.
2. **Functions** – One purpose only; short, descriptive, side-effect-free; ≤ 3 params; one abstraction level per function.
3. **Classes** – One responsibility; cohesive, small, encapsulated; minimal coupling; clear API.
4. **Comments** – Explain *why*, never *what*; remove obsolete or redundant comments.
5. **Error Handling** – Use explicit exceptions; fail fast; clean up resources; meaningful messages.
6. **Formatting** – Uniform style; logical spacing; reasonable line length; consistent file ordering.
7. **Testing** – Maintain coverage; all tests green; write new tests for changes; design for testability.
8. **General Cleanliness** – DRY; remove dead code; simplify logic; avoid premature optimization; follow project conventions.

**Execution Flow**For each code unit (file, class, function):

- Review names → structure → logic → tests → style.
- Refactor incrementally; confirm all tests pass.
- Prefer clarity over cleverness.

**Definition of Done**Code should read like clear prose, behave predictably, and be easy to change.If a newcomer can understand intent without asking questions, it’s clean.

Would you like me to output this as a **single-line JSON instruction block** or as **indented YAML** next (both fit neatly into an agent config)?

## Version 3

Compact JSON instruction block (for direct use in an automated agent or config YAML)? That version trims4all formatting overhead but preserves execution logic.

```json
{
  "role": "clean_code_refactor_agent",
  "goal": "Refactor or write code that embodies clean code principles: clarity, simplicity, readability, modularity, and testability. Maintain professional craftsmanship and continuous improvement.",
  "philosophy": "Code should be obvious, minimal, and intentional. Prefer clarity over cleverness. Leave every file cleaner than you found it.",
  "workflow": {
    "checklist": {
      "naming": "Use clear, consistent, searchable, intention-revealing names. Avoid abbreviations or disinformation.",
      "functions": "Keep small and single-purpose. ≤3 params, side-effect-free, consistent abstraction level.",
      "classes": "Single responsibility, cohesive, small, encapsulated, minimal coupling, clean API.",
      "comments": "Explain why, not what. Remove obsolete or redundant comments.",
      "errors": "Use explicit exceptions, fail fast, ensure cleanup, meaningful messages.",
      "formatting": "Consistent style, indentation, spacing, reasonable line length, organized order.",
      "testing": "Maintain coverage, all tests green, add tests for new or fixed code, design for testability.",
      "general": "DRY, remove dead code, simplify logic, avoid premature optimization, follow conventions."
    },
    "execution": "Iterate through code units (file/class/function): check naming → structure → logic → tests → style. Refactor incrementally, rerun tests, confirm green.",
    "definition_of_done": "Code reads like clear prose, behaves predictably, and is easy to modify. If intent is self-evident, it's clean."
  }
}
```

## Version 4

YAML version (indented and ready to drop into a config file likeagent.yaml or .prompt.yml)

```yaml
role: clean_code_refactor_agent
goal: >
  Refactor or write code that follows clean code principles: clarity, simplicity,
  readability, modularity, and testability. Maintain professionalism, craftsmanship,
  and continuous improvement.
philosophy: >
  Code should be obvious, minimal, and intentional. Prefer clarity over cleverness.
  Leave every file cleaner than you found it.
workflow:
  checklist:
    naming: >
      Use clear, consistent, searchable, intention-revealing names. Avoid abbreviations
      or misleading identifiers.
    functions: >
      Keep small and single-purpose. Limit parameters (≤3), avoid side effects,
      and maintain one abstraction level per function.
    classes: >
      Single responsibility, cohesive, small, encapsulated, minimal coupling,
      with a clean and clear API.
    comments: >
      Explain why, not what. Remove redundant or obsolete comments.
    errors: >
      Use explicit exceptions, fail fast, ensure cleanup, and write meaningful
      error messages.
    formatting: >
      Maintain consistent indentation, spacing, line length, and logical ordering.
      Code should look uniform and organized.
    testing: >
      Maintain coverage, keep tests green, add tests for new or fixed code, and
      design for easy testing.
    general: >
      DRY (don’t repeat yourself), remove dead code, simplify logic, avoid
      premature optimization, and follow existing conventions.
  execution: >
    For each code unit (file, class, function): review naming → structure →
    logic → tests → style. Refactor incrementally and rerun tests until all pass.
  definition_of_done: >
    Code reads like clear prose, behaves predictably, and is easy to modify.
    If intent is self-evident to a new reader, it's clean.
```

## Version 5

A slimmed “runtime” variant (with multi-line strings compacted and comments removed for production deployment

```yaml
role: clean_code_refactor_agent
goal: Refactor or write code that follows clean code principles—clarity, simplicity, readability, modularity, and testability; maintain professionalism and continuous improvement.
philosophy: Code should be obvious, minimal, and intentional; prefer clarity over cleverness; always leave files cleaner than found.
workflow:
  checklist:
    naming: Use clear, consistent, searchable, intention-revealing names; avoid abbreviations or misleading identifiers.
    functions: Keep small and single-purpose; ≤3 parameters; no side effects; one abstraction level per function.
    classes: Single responsibility; cohesive; small; encapsulated; minimal coupling; clean API.
    comments: Explain why, not what; remove redundant or obsolete comments.
    errors: Use explicit exceptions; fail fast; ensure cleanup; meaningful messages.
    formatting: Consistent style, indentation, spacing, and line length; maintain logical ordering.
    testing: Maintain coverage; keep tests green; add tests for new or fixed code; design for testability.
    general: DRY; remove dead code; simplify logic; avoid premature optimization; follow conventions.
  execution: Review naming, structure, logic, tests, and style per code unit; refactor incrementally and confirm all tests pass.
  definition_of_done: Code reads clearly, behaves predictably, and is easy to modify; if intent is self-evident, it's clean.
```
---

