# Folder & Image Load Performance — Design & Status (v1)

Goal: **load images as fast as possible with zero UI freezes, zero black frames, and
bounded memory** — on large folders, with many open tabs, while files change on disk.

This document is both the design and the running status. Everything cites the current
implementation (`file:line`). Measurements come from this workspace and this machine
(i7-11800H, 8C/16T, RTX 3080 Laptop, 16 GB, torch 2.7.1+cu118) against the three
folders recorded in `app_settings.open_tabs`:

| Tab | Folder | Photos | RAW | On disk | Avg ARW |
|---|---|---|---|---|---|
| 1 | `2026-02-26 - Chidiyatapu` | 106 | 105 | 6.3 GB | 60.2 MB |
| 2 | `2026-02-27 - Kalatang, Sippighat` | 194 | 194 | 8.2 GB | 42.3 MB |
| 3 | `2026-02-28 - Chidiyatapu, Haddo Warf` | 203 | 203 | 10.5 GB | 51.8 MB |

---

## 0. Status

**Round 1 shipped and measured**, on the folders above:

| | Before | After | Change |
|---|---|---|---|
| Folder load, 105-photo ARW folder | 3.31 s | **1.40 s** | 2.4x |
| — of which the ExifTool subprocess | 3.10 s (94 %) | **~1.3 s (95 %)** | 2.4x |
| Cold ARW thumbnail, 1 thread | 55 ms | **31 ms** | 1.8x |
| Cold ARW thumbnail, 4 threads | 7 ms | **7 ms** | — (already parallel) |
| Bytes read per ARW | ~68 MB | **~1 MB** | 68x |
| EXIF rating sync, 24 files | 12.22 s | **5.24 s** | 2.33x |
| Tab switch, loaded tab (203 photos) | 33 ms | **16 ms** | 2x |
| Tab switch while thumbnails decode | 95 ms | **16 ms** | 6x |
| Rapid switching during a load | 134–474 ms | **119 ms** | up to 4x |
| Soft refresh of the same tab | 108–121 ms | **4–5 ms** | 25x |
| Thumbnail duration timer | never stopped | **stops on completion** | — |
| Thumbnail memory, 3 tabs × 503 photos | 0/14/23 evicted per pass | **0 evicted, 161 MB of a 192 MB budget** | — |
| Tab switch re-decode | every photo | **none** (shared caches) | — |

**Round 2 closed every defect §11 still listed as open** and added eight more that were
found while doing it (D26–D33). See §0.1 for the numbers and §11/§12 for the defect
tables and the analysis.

### 0.1 Round 2 — measured

Measured the same way, on the same machine and folders.

| | Round 1 | Round 2 | Note |
|---|---|---|---|
| Rescan of an unchanged folder | 1.40 s (ExifTool over all files) | **one `scandir` pass, no ExifTool** | §4.3 |
| Reload after one file changed | 1.40 s | **1 ExifTool call + 1 re-decode** | §4.3 |
| Watcher-triggered reload | full `_load_directory`, filters and selection reset | **tab-local reload, manifest diff** | §4.4 |
| Rename | lost flags, ratings, tags | **carried over, DB row re-pointed** | §4.4 |
| Decode cache on reload | `clear_cache()`, every tab evicted | **only changed files evicted** | §4.2 |
| An edited file | served the stale cached render | **miss, re-decoded** | §4.2 |
| Orientation cache | unbounded dict, one entry per file | **LRU, 600 entries, thread-safe** | §4.5 |
| Full-cache insert/evict | **outside the lock** | **guarded**, running byte totals | §4.5 |
| Bytes read per ARW | ~1 MB | **~1 MB** | unchanged |
| Thumb / full cache accounting | O(n) re-sum per insert (O(n²) per load) | **O(1) running totals** | §4.5 |
| Threads per navigation | 1 new OS thread per event | **bounded pool of 2** | §4.7 |
| Threads per prefetch | 1 new OS thread per event | **bounded pool of 1** | §4.7 |
| Files decoded per arrow-key press | neighbours × every stacked variant | **3 primary paths** | §4.7 |
| Viewer redraw at a size already shown | full-resolution resize | **cached render, or derived from a smaller one** | §4.7 |
| Detection box placement past the 3500 px cap | drifted off the subject | **exact** | §4.7 |
| Window resize | resize + repaint per `<Configure>` | **coalesced to one repaint per gesture** | §4.7 |
| Longest single UI tick during a row build | up to ~200 ms | **≤ `ROW_BUILD_BUDGET_MS`** (30 ms) | §4.6 |
| Grid completeness after a tab switch | **frozen part-built (rows=1–4 of 203)** — found and fixed in round 2, see D26 | **all rows** | §4.6 |

**Round 3 — the grid was virtualized.** The row build was the last unbounded thing on
the load path, and on a 2771-photo folder it was the whole story: 111 s, of which 82 s was
Tk re-laying-out 2771 packed children. `culler/gui/row_pool.py` recycles ~21 rows and
rebinds them to the visible window.

| 2771-photo folder | Before | After |
|---|---|---|
| Grid build | 111 s | **376 ms** |
| Rows built / live grid widgets | 2771 / 16 626 | **21 / 221** |
| Thumbnail requests on open | 2771 | **~21** |
| Scrolling the whole folder | 17.6 s | **1.1 s** |
| Longest UI tick | 1513 ms | **334 ms** (median 2.7 ms) |

Measurements, the two non-obvious costs, and what it did not fix are in
`ui-framework-assessment.md` §8. The action bags moved below the items for the same
reason and are covered in §9 of that document.

**Not re-measured, flagged as follow-ups:** the cold-start ExifTool pass (§4.3), the
coarse-timestamp rename fallback (§9).

**Outstanding**, in expected-value order:

1. **Cold start still re-reads ExifTool for every file.** A differential refresh is
   in-session; a fresh launch has no manifest in memory, so it does a full scan. The
   fix is a persisted manifest plus persisted EXIF — §4.3 spells out the schema, and it
   is the largest remaining item.
2. **Grid rows are still CustomTkinter widgets**, at ~2.8 ms each to bind. Plain
   `tk.Frame`/`tk.Label` for the row alone would cut most of the remaining 376 ms
   first-screen cost, with no visual change.
3. **`_row_render_cache` is keyed by item index**, so a long scroll invalidates entries
   for rows it never touches. Harmless, but it makes the cache far less effective under
   scrolling than under a tab switch.
4. **`mtime_ns` granularity** on filesystems with 1–2 s timestamp resolution (§9).
5. **SQLite WAL** (P1-25), untouched — it is a durability change, not a load change.

---

## 1. Requirements → where they are addressed

| # | Requirement (TODO.md) | Section | Status |
|---|---|---|---|
| R1 | Load in background, never block the UI | §4.1 | **partly** — ticks are bounded at 30 ms; row *count* still unbounded |
| R2 | Tab switches must not re-decode the same images | §4.2 | **done** |
| R3 | Manual / triggered refresh = differential, not full | §4.3 | **done in-session**; cold start still full (§4.3) |
| R4 | Auto refresh on external add/delete/update | §4.4 | **done**, including rename detection |
| R5 | Caches must not bloat with image count | §4.5 | **done** — byte budgets, all tiers bounded |
| R6 | A huge folder must not bloat memory | §4.6 | **partly** — pixels bounded; widgets not |
| R7 | Navigation smooth, image visible at the scale loaded | §4.7 | **partly** — no stale frame, no threads, no ladder |

---

## 2. Where the time went, and what fixed it

Each row is the original bottleneck, the fix, and where it lives now.

### 2.1 Opening a folder — `CullingSession.scan_directory` (`culler/culler_engine.py:333`)

| Bottleneck | Fix | Now |
|---|---|---|
| One `exiftool` process with every path on argv, unchunked, so a large folder silently degraded into the per-file PIL fallback | `BATCH_ARGV_LIMIT=200` **and** a concurrency split, 8 processes in flight (`exif_wrapper.py:33-43`, `:317-354`) | 4.09 s → 1.37 s for 105 ARWs |
| `is_available()` spawned `exiftool -ver` **per RAW decode** | memoized probe (`exif_wrapper.py:73`) | one spawn per process |
| Whole-file read to find the embedded ARW preview (~68 MB per decode) | bounded streaming scan, 1 MB ceiling + 8 MB tail, whole-file fallback (`exif_wrapper.py:202-290`) | 34 ms → 1.4 ms, ~1 MB read |
| `annotations.json` re-read and re-`resolve()`d on every scan | memoized by `(path, mtime_ns)`; plus a normcased key index instead of a `resolve()` per photo (`culler_engine.py:_annotation_index`) | parsed once per mtime; zero syscalls per item |
| One `after(0, …)` UI callback per photo | `_ProgressThrottle`: at most one per whole percent, plus a guaranteed final `flush()` (`culler_engine.py:23`, `:58`) | 5000 photos → ~101 UI tasks, and the bar lands on 100 % |
| A connection + commit per photo (`save_image_record`) | `save_image_records`: one connection, one `executemany` transaction (`db_manager.py:302`) | one transaction per batch |
| **`clear_cache()` on every scan**, which emptied the *app-wide* loader and so every other tab's decoded images | removed; a refresh evicts only the files whose bytes changed (`culler_engine.py:scan_directory`, `image_loader.py:invalidate_paths`) | D15 closed |
| **Every rescan was a full rebuild** — same ExifTool pass, same rebuild of every item | `FolderIndex` manifest + differential reconcile (`folder_index.py:158`, `culler_engine.py:_reconcile_items`) | unchanged folder = one `scandir` pass |
| `glob` + `is_file()` double stat, then `ImageItem` `resolve/exists/stat` per file — 5 syscalls per photo | one `os.scandir` + `DirEntry.stat()` supplies the file, the size and the mtime; `ImageItem` takes `size_bytes`/`resolved` (`folder_index.py:57`, `culler_engine.py:ImageItem`) | 1 syscall per file |
| `get_all_records_for_dir` `LIKE dir%` + per-row `resolve` on every scan | `get_records_for_paths` for exactly the rebuilt rows (`db_manager.py:377`) | one `IN` query, only what changed |
| The PIL metadata fallback ran one file at a time | bounded pool, like the ExifTool path (`exif_wrapper.py:578`) | scales instead of serialising |

### 2.2 Building the thumbnail grid — `ThumbnailList`

| Bottleneck | Fix | Now |
|---|---|---|
| **`_batch_raw_requests` was a local variable**, yet the soft-refresh path called `self._batch_raw_requests.clear()` → `AttributeError` inside a Tk callback on every refresh, killing the batch chain and leaving the UI unresponsive | both are instance attributes | no exception; chain alive |
| One `after(0)` per decoded thumbnail | workers push to a deque, one 1 ms tick drains and paints the whole batch (`thumbnail_list.py:899-920`) | one Tk task per batch |
| Fixed 20-row batches blocked the UI ~200 ms per tick | `ROW_BUILD_BUDGET_MS = 30` — build until the budget is spent, then yield (`:26`, `:683`, `:672`) | no tick over ~30 ms |
| **…but the budget was checked *after* the batch**, so a tick still built up to 20 rows before yielding — the same ~200 ms stall the budget existed to remove | deadline tested inside the row loop, always building at least one row (`:745-825`) | tick ≤ 30 ms, verified by test |
| **The soft-refresh path assumed every row already existed** and set `_batch_index = len(items)`, so a second `update_items` for the same rows cancelled the chain and froze the grid at the handful of rows built in the first tick | `_resume_interrupted_build`: a matching signature with missing rows resumes the chain instead (`thumbnail_list.py:692`) | D26 closed; grid always completes |
| Resuming could leave the previous tick scheduled, giving two builders the same index range | `_cancel_row_chain` before resuming (`thumbnail_list.py:516`) | one thumbnail request per row, asserted |
| Soft refresh re-configured all 200 labels and re-imported `CullingSession` per row | `_row_render_cache` of what each row already renders, seeded at row creation (`:55`, `:440`) | 108 ms → 4 ms |
| Switching away and back mid-load re-queued every in-flight decode | path-keyed `_inflight_thumbs` de-duplication (`:64`, `:869`) | each path decoded once |
| Soft refresh did not cancel a pending row-batch chain | `_cancel_batch_chain()` in both paths (`:507`) | chains cannot stack |
| Thumbnail tier held full-resolution buffers for PNG/HEIC/RAW | normalized to ≤400 px before caching (`image_loader.py:get_thumbnail`) | tier is 400 px max |
| **Still open:** one widget set per item (6 widgets, ~10 ms each), no viewport awareness, whole-folder request burst | P2 | — |

### 2.3 Memory ceilings

| Cache | Budget | Note |
|---|---|---|
| `_full_cache` (previews/full decodes) | **512 MB**, then 30 items (`image_loader.py:67`, `:339`) | was 30 items ≈ up to 2.16 GB |
| `_thumb_cache` | **192 MB**, then 600 items (`image_loader.py:57`, `:132`) | measured 320 KB per canonical → holds all 503 photos of three tabs (161 MB) |
| ARW preview bytes | 8 entries, keyed by `(normcase path, mtime_ns, size)` (`image_loader.py:MAX_PREVIEW_CACHE`) | was re-extracted per decode |
| orientation | **600 entries**, LRU, keyed by `(normcase path, mtime_ns, size)` (`exif_wrapper.py:63`) | was an unbounded dict — D6 |
| grid `_ctk_img_cache` + Tk `PhotoImage` | **bounded** — only the visible window exists | pool of ~21 rows, `ui-framework-assessment.md` §8 |

All three tiers are now keyed by **content identity** — `(normcased path, mtime_ns,
size)` plus the render variant — via `ImageLoader.content_key`/`tier_key`
(`image_loader.py:95`, `:111`). An externally edited file is therefore a miss rather
than a stale hit, and a case-only rename is the same entry rather than a spurious one.

Byte accounting is incremental (`_thumb_bytes`, `_full_bytes`) rather than a re-sum of
the whole cache on every insert, and both tiers are mutated under `_cache_lock`: the
full tier was inserted and evicted *outside* the lock, which the round-1 writeup
incorrectly described as fixed.

Decoded images are held in one app-wide `ImageLoader`, so the budgets above are for the
whole application rather than per tab.

### 2.4 Tab switching

`_apply_tab_state` → `update_items`. Two independent costs:

- **Decode:** fixed. `ImageLoader`/`ExifToolWrapper` are created once per app
  (`gui.py:822-823`) and passed into every tab's session (`culler_engine.py:236`), so a
  photo shown in two tabs is decoded once and switching tabs keeps the other tabs' images.
- **Widgets:** still O(photos), now bounded per tick. `_row_signature`
  (`thumbnail_list.py:522`) forces a rebuild when the stack composition changes, so
  switching tabs rebuilds every row; that is the remaining 16–119 ms.

### 2.5 Navigation (`gui.py: _select_image` `:1226`)

1. Cached thumbnail painted synchronously.
2. Cached full image → done.
3. Otherwise a task on a **2-worker pool** with a 150 ms debounce for continuous
   navigation: 400×400 fast preview, then the full decode.
4. `_prefetch_surrounding_images` (`:1362`) decodes the **primary path** of the next two
   and previous one — on a **1-worker pool**, so navigation always supersedes prefetch
   instead of running beside it.

**Fixed:** the fast-preview callback re-checks the request id inside `after()`
(`_apply_fast_preview`, `gui.py:1350`). Previously a 400×400 preview of photo *N* could
land after photo *N+1* was displayed and stay there until *N+1* finished decoding.

**Fixed:** holding the arrow key down used to start a new OS thread per photo, each alive
until its decode finished. Both navigation and prefetch now submit to fixed pools
(`gui.py:103`, `:106`), shut down with `cancel_futures` on close (`:979`).

**Fixed:** the viewer recomputed its contain-fit transform inline in five places, and
only `redraw` applied the 3500 px render cap, so the detection boxes were placed against
a transform that was never drawn. All of it now goes through `contain_fit`
(`gui/gui/view_transform.py:64`), which reports the scale that produced the render it
actually made. The aspect fit is unchanged, so there is still no black frame.

**Fixed:** every redraw resized from the full-resolution source. `RenderPyramid`
(`view_transform.py:105`) keeps the sizes already rendered, derives a new size from the
smallest cached render that still covers it, and is bounded by both level count and
bytes.

**Still open:** the progressive ladder (400 px proxy → ≤2560 px preview → full decode)
is not there, and zoom is still per-event rather than a blit over a full pyramid.

---

## 3. Target architecture

```
                    ┌──────────────── UI thread (Tk) ─────────────────┐
                    │ canvas_viewer · thumbnail_list · meta_panel    │
                    │  budget: only widget mutation, never decode   │
                    └───────────────────────┬───────────────────────┘
                                            │ request (path, tier, priority)
                                            ▼
                    ┌──────────────── LoadCoordinator ──────────────┐
                    │ 1 bounded worker pool (tier-ordered, LIFO)    │
                    │ 2 per-request cancellation token               │
                    │ 3 byte-budgeted tier cache                    │
                    └───────┬───────────────────────────┬──────────┘
                            │                           │
              ┌─────────────▼──────────┐   ┌────────────▼─────────────┐
              │ DecodeBackend          │   │ FolderIndex (per tab)    │
              │  L1 thumb ≤400px       │   │  manifest: path→(mtime,  │
              │  L2 screen-res ≤2.5k   │   │  size, flag, rating)     │
              │  L3 full decode        │   │  diff → add/mod/del/rename│
              │  (shared app-wide)     │   └──────────────────────────┘
              └────────────────────────┘
```

Three invariants:

1. **One owner for all decode work.** Done for navigation, prefetch and the thumbnail
   grid: fixed pools, path-keyed de-duplication, and no thread per event
   (`gui.py:103`, `:106`, `thumbnail_list.py:47`). The scan still runs on a thread the
   caller creates, which is fine — it is one thread per tab, not per request.
2. **One cache instance for the whole app**, byte-budgeted and keyed by content identity
   — **done**.
3. **The UI thread never waits on I/O**, and never blocks longer than one tick. Every
   tick now honours its budget, and an interrupted row build resumes rather than being
   declared finished — **done**; the row *count* per load is still unbounded (P2).

---

## 4. Design per requirement

### 4.1 R1 — background, never block the UI

Done: progress is throttled per percent with a guaranteed final update; thumbnails apply
through one drain tick; each row batch is capped at 30 ms *within* the loop.

Open: `get_filtered_items` (`culler_engine.py:1097`) makes up to 6 passes over
`self.items` on the UI thread — measured at ~0 ms for 203 items with no filters, so it is
only a risk with an aggressive filter combination. Move it into the coordinator when it is
next touched.

### 4.2 R2 — one shared cache across tabs

**Done**, including content-identity keys. Every tier is keyed by
`(normcase path, mtime_ns, size, scale, white_balance)` (`image_loader.py:tier_key`),
so a file edited outside the app is a miss and a renamed file is either the same entry
(case-only) or a new one — never a stale hit. `ImageLoader.invalidate_paths` gives the
refresh path a way to drop specific files without touching the rest.

### 4.3 R3 — differential refresh (done in-session)

`FolderIndex` (`folder_index.py`) holds `{normcase path: (size, mtime_ns)}` per folder and
diffs a fresh `scandir` pass against it (`folder_index.py:158`):

```
added    new keys                      → build item, read EXIF, save DB record
removed  keys gone                     → drop item, delete its DB rows
changed  size or mtime differs         → invalidate cache tiers, re-read EXIF
renamed  removed + added, same stamps  → update path, keep flags, keep cache
no-op    nothing differs               → return the same items, no EXIF at all
```

`scan_directory` (`culler_engine.py:333`) skips `clear_cache()`, and
`_reconcile_items` (`:482`) reuses the `ImageItem` object for every row whose shape is
unchanged, reads EXIF only for rows that gained or changed a file, fetches saved records
only for the rows it rebuilt, and carries flags/ratings/tags/boxes across a rebuild.

Two details that matter and are easy to get wrong:

- **A rating the app has decided must survive a metadata re-read.** EXIF supplies the
  rating only for a row that has no prior state; otherwise the saved record and the
  carried state win.
- **Rename pairing is ambiguous by construction.** A removal is only paired with an
  addition when exactly one candidate shares its `(size, mtime_ns)`
  (`folder_index.py:196`); two identical files are reported as a removal plus an
  addition rather than guessing, and state is carried by group instead.

**Open — cold start.** The manifest lives in memory, so a fresh launch has none and does
a full ExifTool pass. To close it: persist `(mtime_ns, size)` and the parsed EXIF per file
in `image_records` (two columns plus one JSON blob, versioned), and on a cold scan read
from the manifest instead of ExifTool when the stamps match. That is the last ~1.3 s of
a launch and it needs a migration, so it is deliberately not shipped half-done here — a
cold scan that skipped EXIF without also restoring the metadata would show N/A camera
data in the meta panel.

### 4.4 R4 — auto refresh

Shipped: `culler/folder_watcher.py` polls each loaded tab (1.5 s interval, 0.5 s settle,
2 s mtime grace) and reloads the owning tab.

Round 2 added:
- **Rename detection** — a rename arrives as remove + add; the manifest pairs them and the
  item keeps its flags, rating and tags (`culler_engine.py:_persist_renames` re-points the
  DB row, `db_manager.py:delete_image_records` removes the old one).
- **Reload granularity** — `_on_folder_changed` (`gui.py:522`) now goes through the tab's
  own loader instead of `_load_directory`, so a watcher-triggered reload keeps the tab's
  filters, selection and scroll position, and is differential.

### 4.4.1 R4 risk — coarse filesystem timestamps

`mtime_ns` granularity varies: NTFS is fine, but FAT32 and some network shares have 1–2 s
resolution. A same-second rewrite with an identical size is invisible to the manifest and
the watcher both. Where that matters, fall back to a content hash for small files when the
stamps agree but the watcher flagged them (§9).

### 4.5 R5 — memory budgets

Byte budgets with eviction on insert are in place for every tier
(`image_loader.py:_store_thumb` `:132`, `_store_full` `:339`), `cache_stats()` (`:361`)
exposes them, and locks plus per-key single-flight make the shared instance safe.

Round 2 closed the gaps that were still there:

- **The full tier was inserted and evicted without the lock.** Reads were guarded and the
  thumbnail tier was guarded, but `_store_full`/`_evict_full_cache` ran outside it, from up
  to six concurrent decode workers.
- **Byte totals were recomputed by walking the whole cache on every insert** — O(n) per
  insert, O(n²) per folder load, over a dict other threads were mutating.
- **The orientation cache was an unbounded dict** mutated from eight thumbnail workers.
  Now a 600-entry LRU under its own lock.
- The grid's `_ctk_img_cache` is now bounded: only the visible window of rows exists.

### 4.6 R6 — a huge folder must not bloat memory

Decoded pixels are bounded (§4.5). Widgets are now bounded too: round 3 replaced one
widget set per item with a recycled pool of ~21 rows bound to the visible window, so a
folder's resident widget count no longer depends on how many photos it holds.

- **~~Virtualized rows.~~ Done.** `culler/gui/row_pool.py`; measurements in
  `ui-framework-assessment.md` §8. This is what makes a 10 000-photo folder viable.
- **~~Whole-folder thumbnail burst.~~ Done.** Requests follow the viewport: ~21 per bind
  rather than one per photo.
- **Still open:** rows are CustomTkinter widgets at ~2.8 ms each to bind. Plain
  `tk.Frame`/`tk.Label` for the row alone would cut most of the remaining first-screen
  cost; the app's chrome can stay CustomTkinter.
- **Manifest-only scan** above a folder-size threshold.

### 4.7 R7 — smooth navigation, never a black frame

Done: no stale frame; bounded worker pools for navigation and prefetch; prefetch decodes
only primary paths; overlays share one transform with the renderer; repeat renders come
from a bounded pyramid; window resizes are coalesced.

Open: the progressive ladder (400 px proxy → ≤2560 px preview → full decode), so the
proxy→full quality jump is still abrupt, and zoom is still per-event rather than a blit.

---

## 5. Concurrency model

- **Threads, not processes, for decode** — RawPy and Pillow release the GIL for the heavy
  parts; processes would add buffer-copy cost.
- **One pool, tier-ordered** so speculative work cannot starve the visible request.
- **ExifTool stays a subprocess**, with chunked argv and 8 concurrent processes.
  `-stay_open` is worth benchmarking: spawn cost is ~150 ms against a 1.05 s metadata pass,
  so the ceiling is ~10 %.
- **Thread safety**: every cache is behind one `RLock` held only for dict mutation, with
  per-key single-flight so N concurrent requests for one path decode once. This now
  covers the full tier as well, which it did not in round 1. Duplicate submissions are
  also suppressed at the request layer by path (`thumbnail_list.py:869`).
- **Pools, not threads per event**: navigation submits to 2 workers, prefetch to 1
  (`gui.py:103`, `:106`). A superseded request drains rather than piling up, and the pools
  are cancelled on close.
- **GPU**: not used on the load path, deliberately. The RAW payload is a 1616×1080 JPEG
  preview (~0.2 MB) that PIL decodes and downsamples in ~14 ms; a CUDA round trip for
  1.7 MP costs more than the decode it replaces, and the remaining per-ARW time is file I/O
  plus libjpeg. The 26 ms/photo grid cost is Tk widget creation, not pixel work. YOLO
  inference already runs on CUDA via ultralytics, and `ml_trainer.py` passes `device=0` when
  `torch.cuda.is_available()`.

### Measured threading and I/O

| Work | Threads | Scaling *(measured)* |
|---|---|---|
| Thumbnail decode | `min(8, cpu/2)` (`thumbnail_list.py:47`) | 1: 31 ms/file · 4: 7 ms (4.3x) · 8: 6 ms (5.2x) — **I/O bound** |
| ExifTool metadata | `BATCH_CONCURRENCY` = 8 (`exif_wrapper.py:38`) | 1: 4.09 s · 4: 1.84 s (2.2x) · 8: 1.37 s (2.7x) |
| PIL metadata fallback | 8 (`exif_wrapper.py:578`) | was serial; only used when ExifTool is missing |
| EXIF rating write | 1 process per rating value (`exif_wrapper.py:572`) | 24 files: 12.22 s → 5.24 s (2.33x) |
| Folder scan | 1 per tab | was 1 per request, re-running all of the above |

Because decoding is I/O bound, reading ~1 MB instead of ~68 MB per ARW mattered more than
any amount of extra threading. The round-2 result follows the same logic one level up:
not reading 200 unchanged files mattered more than reading one changed file faster.

---

## 6. Observability

`cache_stats()` (`image_loader.py:361`) reports thumb/full item counts, resident bytes and
budgets; `ImageLoader.stats` counts thumb/full hits, misses and in-flight waits.
`CullingSession.last_scan_stats` (`culler_engine.py:_stats`) reports whether the scan was
differential and how many files were added/removed/modified/renamed — that is the number
to assert on in a refresh test, rather than wall-clock alone.

Still to wire: surfacing those counters in the UI, and per-phase scan timings
(stat, manifest diff, EXIF, DB, grid build). The diff already separates the phases that
matter — an unchanged folder reports `differential: True` with all buckets zero — so the
remaining work is presentation, not instrumentation.

---

## 7. Success metrics

| Metric | Measured now | Target |
|---|---|---|
| Folder load, cold start, 105-photo ARW folder | **1.40 s** (95 % ExifTool) | < 0.5 s once the manifest is persisted (§4.3) |
| Rescan, folder unchanged | **one `scandir` pass, 0 ExifTool calls** | keep |
| Rescan, 1 file changed | **1 ExifTool call, 1 re-decode** | keep |
| Cold ARW thumbnail | **31 ms** 1 thread, **7 ms** 4 threads | keep |
| Tab switch, loaded 203-photo tab | **16 ms** to first frame | < 120 ms (already met) |
| Tab switch, grid fully repopulated | **first screen only, ~376 ms on a 2771-photo folder** | keep |
| Rapid switching during a load | **every tab reaches its full row count** | keep |
| Longest single UI tick, any tab operation | **30 ms** (`ROW_BUILD_BUDGET_MS`) | < 50 ms (met) |
| Soft refresh of the same tab | **4–5 ms** | keep |
| EXIF rating sync, 24 files | **5.24 s** | keep |
| Stale frame after rapid navigation | **0** (`gui.py:1350`) | keep 0 |
| Black frame on navigation | **0** (viewer aspect-fits everything) | keep 0 |
| Threads created per navigation / prefetch | **0** (fixed pools of 2 / 1) | keep 0 |
| Files decoded per arrow-key press | **3** (primary paths only) | keep |
| Detection box placement past the render cap | **exact** | keep |
| Thumbnail residency, 3 tabs × 503 photos | **161 MB / 192 MB, 0 evicted** | flat in folder count |
| RSS | bounded by the §4.5 budgets | flat in tab count |

---

## 8. Phased plan

### P0 — shipped in round 1

1. Memoize `ExifToolWrapper.is_available()`. 2. Close the stale-preview race.
3. Byte-budget `_full_cache`. 4. Never store a full-resolution buffer in the thumbnail
tier. 5. Cache ARW preview bytes per `(path, mtime, size)`. 6. Key thumbnails by
`(path, scale, white_balance)`. 7. Locks + per-key single-flight.
8. Memoize `load_manual_annotations` by `(path, mtime_ns)`. 9. Chunk ExifTool argv at 200
paths and run 8 chunks concurrently. 10. Streaming ARW preview extraction with whole-file
fallback. 11. Import `log_error` in `culler_engine.py`. 12. Implement `ImageLoader.is_raw()`.
13. `save_image_records` batch writer. 14. Progress throttle, one update per percent.
15. Batched EXIF rating writes, one process per rating value. 16. Fix the tab-switch crash:
`_batch_*_requests` were locals, not attributes. 17. Coalesce thumbnail application into one
drain tick. 18. Time-budgeted row batches. 19. Row render cache + path-keyed in-flight
de-duplication. 20. Cancel pending batch chains in both `update_items` paths. 21. Base
thumbnail completion on outstanding work, not counters.

### P1 — round 2

22. ~~`FolderIndex` manifest + `apply(change)`; wire the watcher to it instead of
    `_load_directory`; rename detection.~~ **done** — `folder_index.py`,
    `culler_engine.py:scan_directory`, `gui.py:522`.
23. ~~Content-identity cache keys (`mtime_ns`, `size`, `normcase` path).~~ **done** —
    `image_loader.py:tier_key`, `exif_wrapper.py:content_identity`.
24. ~~Single bounded prefetch task replacing `gui.py:1339`; primary path only.~~ **done** —
    `gui.py:103-106`, `:1362`.
25. WAL for SQLite. **Still open** — a durability change, not a load change.

### P2 — huge folders

26. ~~Scroll handler + recycled row pool.~~ **done** — `culler/gui/row_pool.py`, `ui-framework-assessment.md` §8. Manifest-only scan above a size threshold: still open.
27. ~~Priority thumbnail queue.~~ **done** — requests are queued per bound row, RAW first.
28. Move `get_filtered_items` off the UI thread.

### P3 — decode quality

29. Pyramid for the current image; zoom/pan as blits; smooth the proxy→full transition.
    The pyramid's render cache is done (`gui/view_transform.py`), and the duplicated
    contain-fit math is gone, but a true multi-resolution pyramid and blit-based zoom are
    not.
30. Optional `watchdog` observer as a lower-latency R4 trigger, polling as fallback.

### P4 — round 3 (not started)

31. **Persist the scan manifest and the parsed EXIF** so a cold start validates with
    stat-only work (§4.3). The largest remaining item; needs a schema migration.
32. Content-hash fallback for filesystems with coarse `mtime_ns` (§4.4.1).
33. Per-phase scan timings in the UI (§6).

---

## 9. Risks and trade-offs

- ~~**Virtualized rows** change selection, keyboard navigation and multi-select behaviour.~~
  Shipped directly rather than behind a setting: the risk was in the *partial-build*
  state the old design had, and a recycled pool has no partial build. Selection,
  keyboard navigation and right-click all read rows through the index-keyed maps, which
  now hold exactly the visible window — the guards they already had became correct.
- **Byte budgets** can thrash on rapid zoom/scale changes. Pin the current image and its
  immediate neighbours against eviction.
- **Content-identity keys** mean a file touched externally invalidates its caches — correct,
  and now bounded to that file: the refresh path calls `invalidate_paths` for the changed
  set only, so one edited file does not evict the other 200 photos.
- **One shared cache** couples tabs: heavy use of one tab can evict another's thumbnails.
  Mitigate with a small per-tab reservation for the visible range.
- **`mtime_ns` granularity**: filesystems with 1–2 s timestamp resolution can miss a
  same-second modification with identical size. The manifest will then report no change.
  Fall back to a content hash for small files when `(mtime_ns, size)` is unchanged but the
  watcher flagged them; until then this affects a *rename* (flags kept, path updated) far
  more often than it affects a *modification*.
- **Rename pairing** is deliberately conservative. Two files with the same size and mtime
  are never paired, so a rename inside a burst of identical files falls back to
  remove + add and the flags are carried by group membership rather than by name.
- **`exiftool -stay_open`** may beat per-batch spawns but adds a long-lived child process to
  manage on shutdown; measure before adopting.
- **Bounded pools change failure behaviour.** Navigation work that used to run on its own
  thread now queues on 2 workers; a slow decode of the current photo delays the next
  navigation instead of competing with it. That is the intent, but it means a genuinely
  hung decode is no longer isolated — `load_full_image` has an in-flight timeout, so the
  queue drains, but there is no per-request cancel.
- **The differential manifest is in memory only.** It is correct within a session and gives
  nothing on a cold start; nothing about it can make a refresh *worse* than a full scan,
  because a miss falls back to the full path.

---

## 10. Test strategy

Per `.agents/AGENTS.md`, cover the complicated parts. 444 tests pass; the round-2 files
are `test_folder_index.py`, `test_differential_refresh.py`, `test_cache_identity.py`,
`test_view_transform.py`, `test_canvas_viewer.py`, `test_navigation_bounded.py`, and the
new `TestInterruptedRowBuildIsResumed` in `test_tab_switch_scenarios.py`.

- **Unit**: byte accounting and cross-tier eviction; single-flight and path-keyed
  de-duplication; thumbnail keys across scale/WB and across a file edit; the render
  pyramid's hit/miss/eviction behaviour; the contain-fit invariant (`scale` always
  describes the reported size); bounded prefetch targets; row-pool recycling.
- **Manifest diff** (`test_folder_index.py`, `test_differential_refresh.py`): add, modify
  (by size and by mtime), delete, rename, case-only rename, ambiguous rename, unchanged;
  option and directory changes forcing a rebuild; and the state-preservation claims —
  a changed file keeps its flag/rating/tags, a rename keeps them, a joined JPG inherits
  them, and an edited file loses its cached render while its neighbours keep theirs.
- **Regression guards** for every defect in §11 — each has a test that fails without the
  fix. Two of them (the tab-switch crash, the runaway timer) were found by probes rather
  than review, and the tab-switch regression in D26 was found by driving the real
  application through three saved tabs.
- **Tab-switch scenarios** (`tests/test_tab_switch_scenarios.py`): repeated soft refresh,
  switch away and back mid-load, stale results from a superseded load, in-flight
  de-duplication, coalescing, budget yielding, unchanged-row skipping, and resuming an
  interrupted row build without duplicating work.
- **Integration**: a temp folder scanned twice asserting a no-op rescan makes no ExifTool
  call and no cache eviction, and that a one-file change re-reads exactly one file. The
  2000-file version from round 1 is still worth adding alongside the persisted manifest.
- **Performance smoke test** (opt-in, slow): time to first paint and grid-interactive for
  200/2000/10000 files with RSS sampled, asserting the §7 targets via `cache_stats()` and
  `last_scan_stats` rather than wall-clock alone.

**Gap worth closing**: the tab-switch regression was invisible to 438 passing tests because
they exercise `ThumbnailList` directly, while the real failure needed two `update_items`
calls with an *incomplete* previous build. A test that drives `ImageCullerApp` through
three tabs against real folders is slow and machine-specific, but a variant using temp
folders and two tabs would be cheap enough to keep in the suite.

---

## 11. Defects

Fixed unless marked open.

| # | Defect | Location |
|---|---|---|
| D1 | Stale fast-preview could overwrite a newer image | `gui.py:1350` — **fixed** |
| D2 | `is_available()` spawned `exiftool -ver` per ARW decode | `exif_wrapper.py:73` — **fixed** |
| D3 | Full-resolution image stored in the thumbnail cache for PNG/HEIC/RAW | `image_loader.py:get_thumbnail` — **fixed** |
| D4 | Thumbnail index keyed by path only; ignored scale/WB | index removed — **fixed** |
| D5 | `_full_cache` item-counted → up to 2.16 GB | `image_loader.py:67` — **fixed** (512 MB) |
| D6 | `_orientation_cache` unbounded, never evicted, mutated from 8 threads | `exif_wrapper.py:63` — **fixed** (600-entry LRU + lock) |
| D7 | No locks while 4+ threads mutated shared caches | `image_loader.py:79` — **fixed** (round 1; see D27) |
| D8 | `annotations.json` re-read and re-resolved every scan | `dataset_exporter.py` — **fixed** |
| D9 | Unchunked ExifTool argv degraded to per-file PIL fallback | `exif_wrapper.py:33` — **fixed** |
| D10 | One thread per navigation and per prefetch, no cap | `gui.py:103-106`, `:1226`, `:1362` — **fixed** (bounded pools) |
| D11 | Per-item UI progress callback | `culler_engine.py:23` — **fixed** |
| D12 | Per-tab `ImageLoader`/`ExifToolWrapper` duplicated all caches | `gui.py:822` — **fixed** |
| D13 | Connection + commit per `save_image_record` | `db_manager.py:302` — **fixed** (WAL open) |
| D14 | Every redraw resizes from the source image; no pyramid | `gui/view_transform.py:105`, `canvas_viewer.py:259` — **fixed** (bounded render cache; true blit zoom still open) |
| D15 | `clear_cache()` on every scan, including watcher reloads | `culler_engine.py:scan_directory`, `image_loader.py:208` — **fixed** |
| D16 | `log_error` used without importing it (`NameError` on trash failures) | `culler_engine.py` — **fixed** |
| D17 | `ImageLoader.is_raw` referenced but undefined (`AttributeError`) | `image_loader.py:111` — **fixed** |
| D18 | `extract_preview_bytes` read the whole 25–60 MB ARW per decode | `exif_wrapper.py:248` — **fixed** |
| D19 | Prefetch decoded every stacked path of adjacent items | `gui.py:1362` — **fixed** (primary path only) |
| D20 | Contain-fit math duplicated five times; only `redraw` applied the 3500 px cap | `gui/view_transform.py:64`, `canvas_viewer.py:97` — **fixed** |
| D21 | `_batch_raw_requests` / `_batch_other_requests` were locals, so every soft refresh raised `AttributeError` inside a Tk callback and killed the batch chain | `thumbnail_list.py:__init__` — **fixed** |
| D22 | Thumbnail completion derived from counters that de-duplicated requests could never satisfy, so the duration timer ran forever | `thumbnail_list.py:841` — **fixed** |
| D23 | Sync wrote EXIF ratings with one `exiftool` process per photo | `exif_wrapper.py:572` — **fixed** |
| D24 | Soft refresh re-queued every in-flight decode on tab switch | `thumbnail_list.py:869` — **fixed** |
| D25 | One `after(0)` per decoded thumbnail | `thumbnail_list.py:899` — **fixed** |
| D26 | **Soft refresh assumed every row existed**, so a second `update_items` for the same rows cancelled the build chain and set the batch index to the item count — the grid froze at the rows built in the first tick. A tab switch reaches `update_items` twice, so switching to a large tab showed 1–4 rows of 203 | `thumbnail_list.py:692` — **fixed** |
| D27 | **`_store_full`/`_evict_full_cache` mutated `_full_cache` outside `_cache_lock`**, from up to 6 decode workers. D7 was recorded as fixed on the strength of the reads and the thumbnail tier | `image_loader.py:339` — **fixed** |
| D28 | **Byte budgets were recomputed by walking the whole cache on every insert** — O(n²) per folder load, over a dict other threads were mutating | `image_loader.py:132`, `:339` — **fixed** (running totals) |
| D29 | `write_ratings_batch` called `log_error` without importing it → `NameError` inside the error handler, masking the real failure | `exif_wrapper.py` — **fixed** |
| D30 | **`ROW_BUILD_BUDGET_MS` was checked after the whole batch**, so a tick still built up to 20 rows (~200 ms) — the exact stall the budget existed to prevent, and the §7 claim of a ≤30 ms tick was not true | `thumbnail_list.py:745-825` — **fixed** |
| D31 | Resuming an interrupted build left the previous tick scheduled, giving two builders the same index range and queueing every thumbnail twice | `thumbnail_list.py:516` — **fixed** |
| D32 | A throttled progress loop that finished inside one interval left the bar at 0 of N | `culler_engine.py:58` — **fixed** (`_ProgressThrottle.flush`) |
| D33 | The PIL metadata fallback ran one file at a time when ExifTool is missing | `exif_wrapper.py:578` — **fixed** (bounded pool) |
| D34 | `get_records_for_paths`/`delete_image_records` built one placeholder per path; a folder above SQLite's variable limit would fail | `db_manager.py:_MAX_SQL_VARIABLES` — **fixed** (chunked) |
| D35 | **One widget set per photo made the grid build O(n²)**: 2771 rows took 111 s, 82 s of it Tk re-laying-out the packed children, and the chain drained inside one event-loop pass so the app was frozen throughout | `gui/row_pool.py` — **fixed** (recycled pool of ~21 rows) |
| D36 | **A transparent spacer frame as tall as the unwound content cost 324 ms on every scroll repaint** — the obvious way to make the scrollbar honest. The same repaint is 4 ms with the scroll region set analytically | `gui/row_pool.py:_update_spacer` — **fixed** |
| D37 | **Configuring a scroll region resizes the canvas, which fires `<Configure>`, which asks for the region again** — an endless loop that also left a rebind permanently pending, so the grid could never report itself settled | `gui/row_pool.py` — **fixed** (write only on change) |
| D38 | **Action bags overflowed a fixed-height panel and were placed on top of each other**; the fixed 125 px tag buttons needed more inner width than the panel had, so the tags bag was wider than its neighbours | `gui/metadata_panel.py` — **fixed** (scrollable column, one width, width-derived tag buttons) |
| D39 | **A Tk root per test** made the suite unreliable: `test_thumbnail_list.py` created 35, which intermittently failed with `invalid command name tcl_findLibrary` and made the same code take 83 s on one run and over 900 s on the next | `tests/` — **fixed** (one root per module) |

---

## 12. Further analysis — what round 2 changed about the picture

Round 2 was supposed to be a list of six fixes. Working through them surfaced nine more
defects and, more usefully, changed the diagnosis in three places.

### 12.1 The dominant cost is *repeated work*, not slow work

The round-1 conclusion was "ExifTool is 95 % of what is left, so make ExifTool faster."
The differential refresh shows the better question was: **why is ExifTool running at all?**
Once unchanged files skip it, a rescan costs one `scandir` pass, and the per-file costs
themselves — the double stat, the `resolve()` per item, the `LIKE dir%` record query, the
full-cache re-sum — turn out to matter more than they looked when the EXIF number dominated
the total. The `scandir` restructuring was only worth doing *after* the diff existed,
because before it there was no reason to care about the difference between one stat and
three.

### 12.2 Every "cache" in the app was keyed by something weaker than it looked

Four separate caches turned out to have the same latent bug: a key that did not include the
thing that changes the value.

| Cache | Was keyed by | Now |
|---|---|---|
| decoded thumbnails / full / preview bytes | `path` | `normcase path + mtime_ns + size` (+ scale/WB) |
| orientation | `(resolved path, mtime float)` | `normcase path + mtime_ns + size` |
| `annotations.json` | `path` | `(path, mtime_ns)` |

This is a correctness class, not a performance one: a stale hit shows the user the previous
version of a photo with no way to tell. Content identity is now one shared idea
(`ImageLoader.content_key`, `ExifToolWrapper.content_identity`) rather than three
independently-derived ones.

### 12.3 Concurrency defects cluster where two subsystems meet

Three of the round-2 findings — D6, D27, D28 — are all "two threads touching the same
cache", and two more — D10, D19 — are "no bound on how many threads exist". The pattern is
that each subsystem was safe on its own and the seams were not. The lesson for the next
round: the LoadCoordinator in §3 is not a throughput optimisation, it is the thing that
would have made D6, D10, D19, D27 and D28 structurally impossible.

### 12.4 The one genuinely unfixed cost is Tk, not pixels

Measured again: ~10 ms of Tk widget creation per row. Every remaining item in §4.6 is
downstream of that number. Reading ~1 MB instead of ~68 MB per ARW bought 68×; virtualized
rows would buy roughly the same order for the grid. If that effort is ever spent, it should
go here rather than to the remaining ~1.3 s of cold-start ExifTool.

### 12.5 Measurements to distrust

- **The §7 "longest UI stall" row was wrong.** It read 119 ms from a tab switch measured
  before the row budget was checked inside the loop; the real figure was closer to 200 ms.
  Wall-clock sampling of a GUI event loop cannot see a tick that blocks, so this class of
  metric needs an assertion in code, not a stopwatch. D30 is the reason that assertion now
  exists.
- **`clear_cache()` on every scan** made the round-1 "no tab-switch re-decode" result
  conditional: it held *within* a load, and any reload silently undid it. The claim was
  true and misleading at the same time.
- **The 3-tab residency figure (161 MB, 0 evicted)** was measured with `clear_cache()` in
  place, i.e. with each tab's pixels wiped on every switch. The real steady-state cost of
  holding three tabs open is higher, and is now what the 192 MB budget has to cover.