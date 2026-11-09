# Agent Orchestration Guidelines

This repository follows a multi-agent workflow for feature implementation, bug fixes, refactoring, and code review.

## Subagents Defined

1. **Developer Subagent** ([`.agents/agents/developer/agent.md`]):
   - **Role**: Lead Developer.
   - **Responsibilities**: Reading code, implementing changes, refactoring, editing files, creating unit tests in `tests/`, and running test commands.
   - **Access**: Write tools enabled (`edit_file`, `write_file`, `run_command`, etc.).

2. **Reviewer Subagent** ([`.agents/agents/reviewer/agent.md`]):
   - **Role**: Senior Code Reviewer & QA Lead.
   - **Responsibilities**: Auditing git diffs, inspecting edge cases, validating GDPR compliance (data minimization, zero PII/card storage), verifying inline keyboard consistency, and running the test suite (`pytest`).
   - **Access**: Read & test tools (`read_file`, `run_command`, `manage_task`).

## Mandatory Workflow

When the user requests implementing a feature, fixing a bug, or updating code in this codebase:

1. **Implementation Phase**:
   - Invoke the `developer` subagent using `invoke_subagent` (`TypeName: "developer"`).
   - Provide a clear and specific prompt detailing the user's requirements, affected modules, and expectations.
   - The developer subagent must implement the changes and verify unit tests.

2. **Review & Verification Phase**:
   - Once the developer completes its task, invoke the `reviewer` subagent using `invoke_subagent` (`TypeName: "reviewer"`).
   - Prompt the reviewer to inspect `git diff`, run `pytest`, audit security/GDPR compliance, and verify UX consistency.
   - If the reviewer requests changes (`CHANGES_REQUESTED`), invoke the developer subagent to address feedback.

3. **User Reporting**:
   - Once the reviewer approves (`APPROVED`), report the outcome cleanly and concisely to the user, providing clickable `file://` links to modified files.
