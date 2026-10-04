# Agent Rules

## 1. Git Commands
Do not run git commands such as `commit`, `push`, `pull`, `merge`, or `rebase` unless explicitly instructed by the user. If a git operation is needed, ask the user first.

## 2. Testing
Always add test cases for complicated features. New functionality should be covered by tests before marking the task complete.

## 3. Implementation Order
Implement features in the CLI first, then expose them in the GUI. Do not implement features directly in the GUI unless they are GUI-specific. This ensures the core logic is validated and reusable before building UI around it.

## 4. Git Operations
- **Do not execute any Git commands** (e.g., `git commit`, `git push`, `git stash`, etc.) unless explicitly instructed to do so by the user.
- **Commit Convention**: Follow standard conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`). And keep the message short one-liners, with multiple changes seperated by '+'.

## Temporary Files
Create temporary files under `_tmp` folder. Remove them after use.

## Technical Architecture & Documentation
Consult the technical specifications and architecture documents in `docs/` before implementing or modifying related features:
- [Scan for Blur & AI Subject Focus](../docs/scan-for-blur.md): 3 blur detection algorithms (Laplacian, 2-level YOLO subject/eye detection, FFT), 4-stage candidate recall, patch grid bokeh protection, auto-flagging, and dual bounding box UI.
- [Scan for Duplicates & Burst Detection](../docs/scan-for-duplicate.md): Perceptual image hashing (dHash/pHash), Hamming distance thresholds, burst grouping, and sharpness-based keeper selection.
- [Thumbnail & Preview Loading/Caching](../docs/thumbnail-preview-cache.md): Core `ImageLoader` LRU RAM cache, GUI widget caching, async directory scanning, folder watcher reload, and multi-tab isolation.
- [YOLO Custom Training Pipeline](../docs/yolo-custom-training.md): End-to-end active learning fine-tuning on user annotations, dataset layout, training triggers, progress modal, and model hot-reloading.

