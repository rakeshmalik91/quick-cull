# Agent Rules

## 1. Git Commands
Do not run git commands such as `commit`, `push`, `pull`, `merge`, or `rebase` unless explicitly instructed by the user. If a git operation is needed, ask the user first.

## 2. Testing
Always add test cases for complicated features. New functionality should be covered by tests before marking the task complete.

## 3. Implementation Order
Implement features in the CLI first, then expose them in the GUI. Do not implement features directly in the GUI unless they are GUI-specific. This ensures the core logic is validated and reusable before building UI around it.

## 4. Git Operations
- **Do not execute any Git commands** (e.g., `git commit`, `git push`, `git stash`, etc.) unless explicitly instructed to do so by the user.
- **Commit Convention**: Follow standard conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).

## Temporary Files
Create temporary files under `_tmp` folder. Remove them after use.
