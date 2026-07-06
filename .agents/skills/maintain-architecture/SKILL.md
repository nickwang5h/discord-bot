---
name: maintain-architecture
description: Triggers when modifying or working on the Discord Bot project. Enforces reading arch.md, testing, and updating the architecture documentation.
---

# Architecture Maintenance Workflow

Whenever you are asked to implement a new feature, fix a bug, or modify the codebase for this project, you **MUST** follow this workflow:

### 1. Read Architecture First
Before making any changes or planning your implementation, you must read the `arch.md` file located in the project root. This ensures you understand the modular design (Cogs), core services, and data flow.

### 2. Complete the Task
Implement the user's request while strictly adhering to the existing architectural patterns (e.g., separating business logic into `cogs/` and foundational services into `core/`).

### 3. Provide Test Output
After implementing the changes, you must verify them. Run the bot or the relevant test scripts (e.g., `test_yt.py`) and capture the output. You MUST provide this test output/verification result in your response to prove the functionality works.

### 4. Update arch.md
Assess if your changes impact the overall system design. If you:
- Added a new Cog or module
- Modified the core services (`core/`)
- Changed how data flows or added new external dependencies
You **MUST** update `arch.md` to accurately reflect the new state of the project.
