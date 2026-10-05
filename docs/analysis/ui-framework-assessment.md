# UI framework assessment: is Tkinter the problem?

Status: **assessed with measurements, then the recommendation was implemented. Keep
Tkinter.** Grid virtualisation shipped — see §8 for the before/after and §9 for the
action bags, which had the same root cause.

Companion to `load-performance-v1.md`. That document tracks the load path; this one
answers a narrower question — *is the UI toolkit the reason a 2771-photo folder freezes?*

Test folder: `D:\Pictures\2026-02-26 - Andaman, Nicobar\TestRAW` — 2771 ARW, 128.8 GB,
~48 MB each. Machine: i7-11800H, 8C/16T, 16 GB, Python 3.12, CustomTkinter on Tk 8.6.

---

## 1. What actually happens on that folder

Measured end to end, not estimated:

| Phase | Time | Verdict |
|---|---|---|
| `scan_entries` (2771 stats) | **0.04 s** | fine |
| `scan_directory` (ExifTool disabled to isolate the scan) | **2.0 s** | fine |
| Grid row build, 2771 rows | **111 s** | **this is the freeze** |
| — inside `_process_next_batch` (widget construction) | 28.9 s (26 %) | see §2 |
| — in Tk between batches | **82 s (74 %)** | see §3 |
| Switch away and back | 23.5 s for 3 rows | see §4 |

Two facts frame everything below:

1. **The scan is not the problem.** 2 s for 2771 files, and 0.04 s of that is stat'ing.
   Every round of work on this repo has been about the *other* side of the load.
2. **The freeze is entirely in the grid**, and 74 % of it is Tk's geometry manager, not
   our widget code.

---

## 2. CustomTkinter costs ~100–300× a raw Tk widget

Per-widget construction, dark mode, 300 widgets each:

| Widget | Tk | CustomTkinter | Ratio |
|---|---|---|---|
| Frame | 0.01 ms | **2.85 ms** | 285× |
| Label | 0.04 ms | 0.54 ms | 14× |
| Button | 0.04 ms | 1.70 ms | 42× |
| CheckBox | — | 3.01 ms | — |
| ProgressBar | — | 1.37 ms | — |
| **One real grid row (6 CTk widgets)** | ~0.1 ms | **8.56 ms** | ~85× |

`CTkFont` is not a factor (0.007 ms).

A `CTkFrame` is not a Tk widget — it is a `Tk` frame containing a nested `CTkLabel` and a
`CTkCanvas` drawable, plus hover/appearance bindings. That is where the constant cost is,
and it is paid once per widget.

**But this is not the main cost.** Raw construction for 2771 rows is 8.56 ms × 2771 =
**24 s**. The measured build is 111 s. CustomTkinter accounts for at most a fifth of the
problem.

---

## 3. The real cost is O(n²) in the number of packed children

Construction time per batch is flat (first 5 batches: 31–41 ms; last 5: 32–39 ms), so
widget cost is not super-linear. The growth is entirely in the ~100 ms per batch that
`_process_next_batch` does not account for — Tk re-laying out every child of a container
that just grew.

That is the classic quadratic: batch *k* costs O(k), so the total is O(n²/2). For 2771
rows that is ≈3.8 M child-visits, and at Tk's ~20 ns per visit it is **≈77 s** — which
matches the 82 s measured to within the noise.

Confirmed by arithmetic rather than by assumption:

```
rows built        2771/2771  in 111.0s   (40.1 ms/row)
batches                799
inside _process_next_batch   28.9s (26%)
root.update() with 2771 rows resident: ~0 ms   (nothing changes once settled)
```

Construction is O(n); **the layout is O(n²)**. Nothing about the toolkit changes that —
it is `pack()` into a container with n children. Any immediate-mode GUI toolkit that
re-lays-out on resize has the same curve.

**Corollary that matters more than the framework:** the row time-budgeting added earlier
(`ROW_BUILD_BUDGET_MS`) is cosmetic. It bounds how much work is *scheduled* per tick, not
how much the event loop does. Measured: 799 batches, and the whole chain drained inside a
**single** event-loop pass, because each 30 ms batch made the next `after(1)` timer
already overdue, so Tk never returned to the user. The tick budget bought fairness
between other callbacks, not responsiveness against this workload.

---

## 4. What is actually wrong in the code (fixed in this round)

These are ours, not the toolkit's:

| Defect | Effect | Status |
|---|---|---|
| `TabBar._get_index_for_widget` walked `winfo_children()` of every tab, per mouse-motion event | O(n) Tk round trips per pixel of drag → lag and jumpy drop targets | **fixed** — id map built once per tab |
| `TabBar._relayout()` re-`pack`ed every button on every add/remove/reorder | O(n²) over a session, with a visible jump each time | **fixed** — new tabs insert before `+`; full re-pack only on remove/reorder |
| Drag hit-test started from `event.widget` (the *binding* widget), not the cursor | drop target drifted while dragging | **fixed** — hit-test from `winfo_containing(x_root, y_root)` |
| Drag motion was unthrottled | one hit-test per motion event | **fixed** — coalesced to one per idle frame |
| `ThumbnailList._queue_thumb_result` called `after()` from 4 decode workers with no lock | unsynchronised Tk command registration → dropped frames, flicker | **fixed** — guarded by `_inflight_lock` |
| A worker that raised left its path in `_inflight_thumbs` forever | load never completed, progress bar stuck, duration timer ran forever | **fixed** — `_failed_thumbs` set, cleared on success |
| `len(self.tabs) <= 1` early-return in `_close_tab` | the last tab could never be closed, so its memory was never released | **fixed** |
| Closing a tab released nothing | session items, decoded pixels and `CTkImage` copies all leaked for the process lifetime | **fixed** — `_release_tab_resources` |
| No Close All | had to close N tabs one at a time to reclaim memory | **fixed** — `✕✕` button in the tab bar |

---

## 5. Options considered

### A. Migrate to Qt / PySide6 — **rejected**

| For | Against |
|---|---|
| Built-in `QListView`/`QAbstractItemModel` virtualisation: only visible rows are ever built. This is the one option that makes 2771 rows *free*. | The 111 s is 74 % Tk geometry; Qt would remove it **if and only if** we virtualise. Painting 2771 rows the same way in Qt is still 2771 rows. |
| Signal/slot replaces the manual `after()` marshalling this app keeps getting wrong (4 of the defects above are thread-into-Tk mistakes). | ~10 kLOC of view-layer rewrite, every widget re-authored, drag-and-drop, tooltips, canvas viewer, YOLO overlay boxes all redone. |
| Real HiDPI and a coherent widget set. | New class of bug for months, for no gain the app can feel. |
| PySide6 LGPL. | Bundling/licensing review, and the app is currently a single-file `pyinstaller` build. |

The decisive point: **Qt's advantage here is virtualisation, not Qt.** A migration buys
virtualisation *if it is done*, and the same virtualisation works in Tk. It would cost a
rewrite to get something we can get for a few hundred lines.

### B. Replace CustomTkinter with plain Tk — **rejected as a root cause, worth keeping in mind**

It would cut row construction from 8.56 ms to ~0.1 ms — 24 s instead of 111 s for this
folder, a 4.6× win on the part we control. But it removes the dark theme the app is built
around, and it does **not** touch the 82 s of quadratic layout. So: real but secondary,
and it trades away the product's look.

### C. Virtualise the grid in Tk — **recommended**

Keep the toolkit, stop creating a widget set per photo. This is the only option that
changes the asymptotics, and §3 shows the asymptotics are the problem.

---

## 6. Recommended work: virtualise `ThumbnailList`

### Design

Replace the one-widget-set-per-item grid with a recycled pool:

```
                 self._pending_items   (2771 ImageItem, no widgets)
                             │
   viewport (≈12–25 rows) ──►│──►  _row_pool: List[_RowWidgets]   (fixed size)
   scroll offset              │       one CTkFrame + 5 children per slot
                             │       slots are rebound to whichever item index
                             │       the scroll position exposes
```

- **Pool size** = `ceil(viewport_height / row_height) + overscan`, ~20–25 rows. Sized on
  `<Configure>` of the scrollable frame.
- **Binding**, not rebuilding: `_row_render_cache` already knows what each row renders, so
  a rebind only reconfigures widgets whose content actually changed.
- **Scroll-driven**: a `<Configure>`/`yscroll` handler computes the visible index range and
  rebinds slots. Total work becomes O(pool) per frame instead of O(n) per layout.
- **Thumbnail requests** follow the visible range, not the folder: viewport first, then
  ±1 screen, then lazily. This also removes the whole-folder decode burst, which for 2771
  ARW currently queues 2771 requests the moment the row build finishes.
- **Selection and flags** already live on `ImageItem`, so a rebind is not a state
  problem. Keyboard navigation becomes index arithmetic over `_pending_items`.
- **Sticky top row** (folder name) and the progress bar stay outside the pool.

### Expected effect

As predicted, within about a factor of two. See §9 for what was measured.

| | Predicted | Measured |
|---|---|---|
| Grid build, 2771 rows | ~0.2 s (pool creation only) | **376 ms** |
| Resident grid widgets | ~150 | **221** |
| Thumbnail requests on open | ~25 visible + margin | **~21** |

### Risk

Highest-risk change in the repo: it touches selection, multi-select, keyboard navigation,
right-click, and drag-to-rectangle in the grid. Mitigation is to keep the current chunked
build behind a setting until the two agree on a golden sequence of actions.

### Sequencing

1. Pool + scroll rebinding behind `GRID_VIRTUALIZATION = False` (default off).
2. Golden-sequence test: a scripted list of selects/clicks/scrolls must produce the same
   `ImageItem` state and the same visible set under both paths.
3. Flip the default; delete the old path.

---

## 7. What is still worth doing

Shipped with the virtualisation:

- **Thumbnails follow the viewport, not the folder.** Requests are queued per bound row,
  so ~21 at a time instead of 2771. This was the item that said "for 2771 ARW this is the
  difference between 2771 concurrent decodes filling the 192 MB budget with rows nobody is
  looking at, and a usable grid".

Still open:

- **Use plain `tk.Frame`/`tk.Label` for grid rows.** The row is the one place with many
  instances; the app's chrome can stay CustomTkinter. Worth ~2.8 ms per bind, so most of
  the 376 ms first-screen cost, with no visual change if the colours are set explicitly.
- **Report a folder-size notice.** The grid is virtualised now, so this is informational
  rather than a warning about a freeze.
- **`get_filtered_items` off the UI thread** (`load-performance-v1.md` §4.1) — still
  bounded work per refresh, but it walks every item on a UI-thread callback.

---

## 8. What shipped, and what it measured

`culler/gui/row_pool.py` keeps a fixed pool of rows and rebinds them to whichever item
indices the scroll position exposes. Row heights vary (a stacked ARW+JPG row is twice as
tall), so the scroll offset maps to an index through a prefix sum of heights rather than
a divide.

Same folder, same machine, after:

| | Before | After |
|---|---|---|
| Grid build, 2771 rows | 111 s | **376 ms** (first screen only) |
| Rows built | 2771 | **21** |
| Live grid widgets | 16 626 | **221** |
| Scroll to any index | ~850 ms | **~270 ms** |
| Scrolling the whole folder | 17.6 s | **1.1 s** |
| Longest UI tick | 1513 ms | **334 ms** (median 2.7 ms) |
| Thumbnail requests on open | 2771 | ~21 |

Two things were found by measurement rather than reasoning, and both are the kind of
thing that would have been guessed wrong:

1. **The spacer frame was the single most expensive thing in the whole feature.** Making
   the scrollbar honest the obvious way — a transparent frame as tall as the unwound
   content — cost **324 ms on every scroll repaint**, because Tk maps and redraws that
   entire region. With the scroll region set analytically and no filler widget at all,
   the same repaint is **4 ms**. The 80x difference was invisible until it was isolated:
   capping the content height at 20 000 px *and* at 5 000 px both still cost ~580 ms,
   which ruled out "it is the height" and pointed at the widget.
2. **Configuring a scroll region resizes the canvas, which fires `<Configure>`, which
   asks for the scroll region again.** That is an endless loop, and it also left a rebind
   permanently pending, so the grid could never report itself settled. Fixed by writing
   the region only when it actually differs, and by guarding the resize handler on a real
   size change.

Two further costs were reduced rather than removed: a scroll rebinds rows *in place*
(reusing the widgets) instead of destroying and recreating them, and every CTk
`configure()` is skipped when the value is unchanged, because each one redraws a canvas.
Together these took a scrollbar jump from ~1.1 s to ~270 ms.

The row build is now bounded by the pool rather than by the folder, which removes a bug
class structurally: there is no partial build left to freeze, because only the visible
window exists and it is bound synchronously on every `update_items`. The tests that used
to guard the interrupted-build defect now assert that invariant instead.

### Design as shipped

```
_pending_items (2771 ImageItems, no widgets)
        |
        |  scroll offset --> prefix sum of row heights --> first visible index
        v
_row_pool: 21 slots, one CTkFrame + 5 children each
        |  slot k renders target[k]; unchanged slots are not touched
        |  rebinds are budgeted per tick and resume on after_idle
        v
thumbnail requests, one per bound row, RAW first
```

- Pool size = viewport + overscan, recomputed on `<Configure>`.
- `after_idle`, not `after(1)`, for the rebind continuation: a rebind tick takes longer
  than 1 ms, so a 1 ms timer is already expired when armed and Tk runs the whole chain
  without returning to the loop — the original freeze, in miniature.
- The index-keyed row maps (`_row_frame_map`, `_btn_map`, `_label_map`) are kept and hold
  only the visible window, so everything that reads a row by item index keeps working and
  its existing "is it bound?" guards become exactly right.

### What it did not fix

- **Cold start still re-reads ExifTool.** The manifest is in memory only; see
  `load-performance-v1.md` §4.3.
- **`_row_render_cache` is keyed by item index**, so a long scroll invalidates entries for
  rows it never touches. Harmless (it is a cache) but it means the cache is much less
  effective under scrolling than under a tab switch.
- **A scrollbar jump is still one ~270 ms operation.** Bounded and once per gesture, but
  not free; it is 21 rows of real widget work.

### 8.1 Round 4 — the regressions virtualisation introduced

Shipping the pool made the app *worse* in three ways, all found by measuring the running
app rather than by reading the code. They are recorded here because the pattern recurs:
every one of them was a piece of state the pool introduced, and every one was invisible
in a test that did not drive a real event loop.

| Symptom | Cause | Fix |
|---|---|---|
| Rows rendered blank — no filename, no stars, no flag colour | registering a bound row popped the `_label_map` entry the body binder had just created | stop popping it; the assertion now checks every bound row has a label showing its filename |
| Thumbnails never appeared and the load never finished | decode workers registered the drain with `after()`; when that failed nothing retried, so results sat in the queue and their paths stayed "in flight" | workers only queue. `_arm_thumb_drain` schedules from the UI thread, driven by the poll that already runs every frame |
| Frozen on arrow keys, at any folder size | `set_selected_indices` drained the pool with an *unbounded* budget, so every press re-bound the whole screen | one tick's worth. **270 ms → 19 ms** median per press |
| Jumpy scrolling | the 60 ms scroll poll requested a full rebind on every *pixel* of movement | rebind only when the visible index range leaves the bound window |
| One 894 ms turn while idle | `scroll_to_index` called `update_idletasks()` to read the scroll position back, forcing a full layout pass on every press | remember the requested offset instead, and let the poll reconcile it |

After the round: idle is **0.02 ms mean, 1 ms worst** per event-loop turn, thumbnails and
labels render, and a load completes (`inflight=0`, `queue=0`).

### 8.2 What is still slow, and it is not the grid

Measured on a 105-ARW folder, one arrow-key press:

| | Median |
|---|---|
| Grid selection change (rebinds, styles, status) | **19 ms** |
| Viewer: full decode | 11 ms |
| Viewer: `set_image` + canvas redraw | 26 ms |
| **`_select_image` end to end** | **356 ms** |

The grid is 5 % of a press. The remaining ~300 ms is the navigation pipeline: a 400 px
proxy decode, then the full decode, then `_prefetch_surrounding_images` decoding three
more primary paths — four RAW decodes per keystroke on a two-worker pool. That is the
progressive ladder §5 was already tracking as open, and it is now the dominant cost of
navigating. It is not a virtualisation regression, but it is the next thing worth fixing:
the prefetch should not decode on every press, and the proxy should be skipped when the
full decode is already cached or already in flight.

### 8.4 Removing the footer progress bar broke the grid — and nothing caught it

Taking the batch count out of the grid footer left three live references to the deleted
`progress_bar` / `lbl_progress_text` in `gui.py`. A reference to a missing widget fails
inside a Tk callback, and Tk prints the traceback and carries on, so nothing *looks*
broken — the code that needed the widget simply never runs.

Two of the three were fatal:

- **`_on_scan_complete`** touched the removed bar *before* `set_image_loader` and
  `_on_filter_changed`. Every restored or opened folder therefore finished its scan and
  never handed the items to the grid: the header read **"Images (0)"**, the grid's image
  loader was never even set, and the folder scan — a 105-photo ARW folder — completed in
  2.6 s into a permanently empty grid.
- **`_sync_loading_progress`** raised on every progress tick, once per percent, flooding
  the event loop with swallowed tracebacks.

`_ProgressThrottle` capped that at ~100 per scan, which is why the app still looked
mostly alive and the tab label still showed its ⟳.

The lesson is about coverage, not about the widget. Every test for the grid exercised
`ThumbnailList` directly, so nothing ever ran `ImageCullerApp`'s completion handlers — the
only place the two halves meet. Two guards now cover it:

- `tests/test_footer_widgets_removed.py` walks the AST of every module and fails if any
  of them so much as mentions a retired widget name, so removing one cannot leave a
  reference behind again.
- The same file asserts that the completion handlers actually reach `_on_filter_changed`,
  which is the statement whose loss produced the empty grid.

Scan progress was not thrown away with the count: it is folder-wide rather than
grid-wide, so it now reports to the status bar instead.

### 8.3 The blank grid past ~32 000 px — open, and it is a platform limit

Navigating away from the first screen can leave the grid blank, and it stays blank. The
cause is structural in the recycled design rather than a slip in it: the rows are packed at
the top of a scrollable canvas whose height is the *whole* list, so on a folder of a few
hundred rows the canvas is tens of thousands of pixels tall while the rows occupy only the
first ~2 000 of them. Scrolling anywhere else shows empty canvas. On the 400-row
reproduction the rows sat at y=0 while the viewport sat at y=19 250.

Two fixes were tried and one of them does not work:

- **Positioning the rows at the scroll offset** with `place` inside a container whose
  height is the content height. This fights `CTkScrollableFrame`, which derives the canvas
  window item from `bbox("all")` on every `<Configure>`, and it hit Tk's own limit: a
  mapped widget cannot be placed further than about **32 767 px** from the top of its
  window. Placing the pool at y=39 150 put it at y=32 767, so the last rows of a long
  folder are unreachable no matter what. Reverted rather than left half-applied.
- **Compressing the scroll space** past a ceiling and mapping the canvas offset back into
  item space. Also reverted: it depends on the positioning working first.

What does work, and is in this change: the window under-fill at the end of a list (the
forward pass ran out of rows and left a partly empty grid), and the `_btn_map` leak.

The real fix is to stop asking the canvas to represent a list longer than the platform
can map. Two viable shapes:

1. **Own the scrollbar.** Keep the scrollable content exactly one screen tall, position
   the rows at y=0 always, and translate the wheel, the arrow keys and a custom scrollbar
   thumb into a first-visible index. Removes the ceiling entirely and makes wheel
   scrolling exact per row. Costs a hand-written scrollbar.
2. **Windowed scrollbar.** Keep `CTkScrollableFrame` but clamp the content height to
   ~30 000 px and map the offset proportionally. Less code, and it degrades wheel
   granularity on long folders — roughly 8 rows per scrollbar pixel on a 2771-row folder.

Both are contained changes to `row_pool.py`. Neither is a one-liner, and guessing at them
again is what produced this section.

---

## 9. The action bags

Not part of the assessment, but the same root cause surfaced there and is fixed.

The six action bags were packed straight into the right sidebar, whose height is fixed by
the grid layout. Once their combined height exceeded the panel, Tk stopped placing the
remainder — which is what put bags on top of each other. Separately, the tag buttons were
a fixed 125 px each, two per row, needing ~258 px of inner width against ~250 px
available, so the tags bag was genuinely wider than its neighbours.

Fixed by keeping them in the right sidebar and putting them in one scrollable column.
The bags keep the arrangement they always had; the change is structural, not visual:

- one scrollable column, so overflow scrolls instead of overlapping;
- every bag packed with identical options, so they share the panel's width;
- tag buttons sized from the panel width, two per row, expanding to fill;
- **each bag's title doubles as a drag handle**, and the order is persisted under
  `meta_panel_bag_order` in `app_settings`. No separate grip widget, so the bags look
  exactly as they did;
- the panel's height is capped at a share of the column, so a short window squeezes the
  panel rather than the grid.

Verified in the running app: panel below the grid, no intersecting bag spans, no bag
wider than the panel, and a drag persisted to the database.

Two smaller defects fixed alongside it:

- **Creating a Tk root per test** made the suite unreliable — `test_thumbnail_list.py`
  created 35 of them. It intermittently failed with `invalid command name
  tcl_findLibrary`, which surfaced as errors and skips rather than real failures, and the
  same code took 83 s on one run and over 900 s on the next. One root per module now;
  493 tests pass in a stable ~85–100 s.
- **`text_color=""` is not valid in CustomTkinter**, so the drag highlight could not be
  cleared. The original colour is captured and restored.

---

## 10. Summary

- The scan is fine: **2.0 s for 2771 files**. Keep optimising that last while it is not
  where the time goes.
- The freeze was **Tk's O(n²) re-layout** of 2771 packed children: **82 s of the 111 s**.
- CustomTkinter makes widget creation ~85× more expensive than raw Tk, which is
  **28.9 s of the 111 s**. Real, but secondary, and the app's theme depends on it.
- The time-budgeting in place bounds scheduled work, not work the event loop does; it
  could not prevent this freeze, and §8 records that a 1 ms rebind timer is worse than
  useless for the same reason.
- **Do not migrate the toolkit.** Virtualising the grid in Tk targeted the actual
  asymptote, and it was a few hundred lines rather than ~10 kLOC.
- It worked: **111 s → 376 ms** to open the grid, **16 626 → 221** resident widgets, and
  the longest UI tick went from **1513 ms to 334 ms**.
- Four of the tab-bar/threading defects fixed earlier were plain implementation bugs in
  code the toolkit made easy to get wrong — a case for tightening those call sites rather
  than changing frameworks.