# Folder & Image Load Performance — Design & Status (v1)

Goal: **load images as fast as possible with zero UI freezes, zero black frames, and
bounded memory** — on large folders, with many open tabs, while files change on disk.

This document is both the design and the running status. Everything cites the current
implementation (`file:line`, verified at commit `a907500`). Measurements come from this
workspace and this machine (i7-11800H, 8C/16T, RTX 3080 Laptop, 16 GB, torch 2.7.1+cu118)
against the three folders recorded in `app_settings.open_tabs`:

| Tab | Folder | Photos | RAW | On disk | Avg ARW |
|---|---|---|---|---|---|
| 1 | `2026-02-26 - Chidiyatapu` | 106 | 105 | 6.3 GB | 60.2 MB |
| 2 | `2026-02-27 - Kalatang, Sippighat` | 194 | 194 | 8.2 GB | 42.3 MB |
| 3 | `2026-02-28 - Chidiyatapu, Haddo Warf` | 203 | 203 | 10.5 GB | 51.8 MB |

---

## 0. Status

**Shipped and measured**, on the folders above:

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

**Outstanding**, in expected-value order:

1. **P1-16 differential refresh.** ExifTool is now ~95 % of what is left in a folder load
   (1.3 s of 1.40 s), and essentially all of a reload caused by a one-file change. This is
   the highest-value item on the list.
2. **P2 virtualized rows.** Row construction is ~10 ms of Tk widget work per photo, so a
   203-photo tab still needs ~2 s of UI-thread work to build every row — now spread across
   ~30 ms ticks, so it no longer freezes but it is still the longest operation.
3. **P1-19 bounded prefetch.** Navigation still spawns a thread per prefetch (§4.7).
4. **Navigation pyramid** (§4.7) and **content-identity cache keys** (§4.2).

---

## 1. Requirements → where they are addressed

| # | Requirement (TODO.md) | Section | Status |
|---|---|---|---|
| R1 | Load in background, never block the UI | §4.1 | **partly** — ticks are bounded at 30 ms; row *count* still unbounded |
| R2 | Tab switches must not re-decode the same images | §4.2 | **done** |
| R3 | Manual / triggered refresh = differential, not full | §4.3 | **open** (P1-16) |
| R4 | Auto refresh on external add/delete/update | §4.4 | **done**, minus rename detection |
| R5 | Caches must not bloat with image count | §4.5 | **done** — byte budgets |
| R6 | A huge folder must not bloat memory | §4.6 | **partly** — pixels bounded; widgets not |
| R7 | Navigation smooth, image visible at the scale loaded | §4.7 | **partly** — no stale frame; no pyramid |

---

## 2. Where the time went, and what fixed it

Each row is the original bottleneck, the fix, and where it lives now.

### 2.1 Opening a folder — `CullingSession.scan_directory` (`culler/culler_engine.py:273`)

| Bottleneck | Fix | Now |
|---|---|---|
| One `exiftool` process with every path on argv, unchunked, so a large folder silently degraded into the per-file PIL fallback | `BATCH_ARGV_LIMIT=200` **and** a concurrency split, 8 processes in flight (`exif_wrapper.py:33-43`, `:317-354`) | 4.09 s → 1.37 s for 105 ARWs |
| `is_available()` spawned `exiftool -ver` **per RAW decode** | memoized probe (`exif_wrapper.py:73`) | one spawn per process |
| Whole-file read to find the embedded ARW preview (~68 MB per decode) | bounded streaming scan, 1 MB ceiling + 8 MB tail, whole-file fallback (`exif_wrapper.py:202-290`) | 34 ms → 1.4 ms, ~1 MB read |
| `annotations.json` re-read and re-`resolve()`d on every scan | memoized by `(path, mtime_ns)` (`dataset_exporter.py:load_manual_annotations`) | parsed once per mtime |
| One `after(0, …)` UI callback per photo | `_ProgressThrottle`: at most one per whole percent (`culler_engine.py:22`, used at `:359`, `:408`) | 5000 photos → ~101 UI tasks |
| A connection + commit per photo (`save_image_record`) | `save_image_records`: one connection, one `executemany` transaction (`db_manager.py:302`) | one transaction per batch |
| **Still open:** `clear_cache()` on every scan (`culler_engine.py:292`), `glob` + `is_file()` double stat (`:297`), `ImageItem` `resolve/exists/stat` per file (`:150-155`), stacked-size recount re-stating every path (`:340`), `get_all_records_for_dir` `LIKE dir%` + per-row `resolve` (`:362`) | P1-16 | — |

### 2.2 Building the thumbnail grid — `ThumbnailList`

| Bottleneck | Fix | Now |
|---|---|---|
| **`_batch_raw_requests` was a local variable**, yet the soft-refresh path called `self._batch_raw_requests.clear()` → `AttributeError` inside a Tk callback on every refresh, killing the batch chain and leaving the UI unresponsive | both are instance attributes | no exception; chain alive |
| One `after(0)` per decoded thumbnail | workers push to a deque, one 1 ms tick drains and paints the whole batch (`thumbnail_list.py:899-920`) | one Tk task per batch |
| Fixed 20-row batches blocked the UI ~200 ms per tick | `ROW_BUILD_BUDGET_MS = 30` — build until the budget is spent, then yield (`:26`, `:683`, `:672`) | no tick over ~30 ms |
| Soft refresh re-configured all 200 labels and re-imported `CullingSession` per row | `_row_render_cache` of what each row already renders, seeded at row creation (`:55`, `:440`) | 108 ms → 4 ms |
| Switching away and back mid-load re-queued every in-flight decode | path-keyed `_inflight_thumbs` de-duplication (`:64`, `:869`) | each path decoded once |
| Soft refresh did not cancel a pending row-batch chain | `_cancel_batch_chain()` in both paths (`:507`) | chains cannot stack |
| Thumbnail tier held full-resolution buffers for PNG/HEIC/RAW | normalized to ≤400 px before caching (`image_loader.py:get_thumbnail`) | tier is 400 px max |
| **Still open:** one widget set per item (6 widgets, ~10 ms each), no viewport awareness, whole-folder request burst | P2 | — |

### 2.3 Memory ceilings

| Cache | Budget | Note |
|---|---|---|
| `_full_cache` (previews/full decodes) | **512 MB**, then 30 items (`image_loader.py:67`) | was 30 items ≈ up to 2.16 GB |
| `_thumb_cache` | **192 MB**, then 600 items (`image_loader.py:57`) | measured 320 KB per canonical → holds all 503 photos of three tabs (161 MB) |
| ARW preview bytes | 8 entries, keyed by `(path, mtime, size)` (`image_loader.py:MAX_PREVIEW_CACHE`) | was re-extracted per decode |
| orientation | per `(path, mtime)` dict | **still unbounded** — see D6 |
| grid `_ctk_img_cache` + Tk `PhotoImage` | **still unbounded** within a load; cleared on a full row rebuild | bounded only once rows are virtualized (P2) |

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

### 2.5 Navigation (`gui.py: _select_image` `:1207`)

1. Cached thumbnail painted synchronously.
2. Cached full image → done.
3. Otherwise a thread with a 150 ms debounce for continuous navigation: 400×400 fast
   preview, then the full decode.
4. `_prefetch_surrounding_images` (`:1339`) decodes the neighbours **and every stacked
   path** in **another new thread**, unbounded in count.

**Fixed:** the fast-preview callback re-checks the request id inside `after()`
(`_apply_fast_preview`, `gui.py:1327`). Previously a 400×400 preview of photo *N* could land
after photo *N+1* was displayed and stay there until *N+1* finished decoding.

**Open:** the viewer aspect-fits everything it receives (`canvas_viewer.py: redraw` `:239`),
so there is no black frame, but every redraw resizes from the full-resolution source
(`:279`) capped at 3500 px (`:272-276`), so the proxy→full quality jump is abrupt and zoom
still re-resizes per event (`:759-771`).

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

1. **One owner for all decode work.** Partly true: the thumbnail pool is bounded and
   de-duplicated (`thumbnail_list.py:47`), but navigation and prefetch still spawn threads
   per event (`gui.py:1207`, `:1339`).
2. **One cache instance for the whole app**, byte-budgeted — **done**.
3. **The UI thread never waits on I/O**, and never blocks longer than one tick — **done for
   thumbnails**, not yet for the row-count problem.

---

## 4. Design per requirement

### 4.1 R1 — background, never block the UI

Done: progress is throttled per percent; thumbnails apply through one drain tick; each row
batch is capped at 30 ms.

Open: `get_filtered_items` (`culler_engine.py:1097`) makes up to 6 passes over `self.items`
on the UI thread — measured at ~0 ms for 203 items with no filters, so it is only a risk with
an aggressive filter combination. Move it into the coordinator when it is next touched.

### 4.2 R2 — one shared cache across tabs

**Done**, except content-identity keys. The cache is keyed by `(path, scale, white_balance)`
(`image_loader.py`), not by `(canonical_path, mtime_ns, size)`, so a renamed file is a miss
and an externally edited file is a stale hit until something re-requests it. The folder
watcher reports `modified` entries, so the fix is to fold `(mtime_ns, size)` into the key
when P1-16 lands.

### 4.3 R3 — differential refresh (open, highest value)

Replace "rescan everything" with a manifest diff. `FolderIndex` per tab holds
`{canonical_path: (mtime_ns, size, item_ref)}`; the item objects already carry flag, rating
and tags, so a refresh does not rebuild them.

```
added    = new_paths  - manifest_keys      → create item, read EXIF, save DB record
removed  = manifest_keys - new_paths      → drop item (+ its DB rows)
changed  = keys where (mtime_ns, size) differ → invalidate cache tiers, re-read EXIF
renamed  = removed ∩ added (same size+mtime, different name) → update path, keep item, keep cache
```

On a refresh: skip `clear_cache()` (`culler_engine.py:292`), skip EXIF for unchanged files
(`:362-409`), skip the stacked-size recount (`:340`), pass the scan's stat into `ImageItem`
(`:150-155`), and persist the manifest so a cold start validates with stat-only work.

The watcher already produces the added/removed/modified sets
(`folder_watcher.py:37 FolderChange`), so `FolderIndex` consumes it directly.

### 4.4 R4 — auto refresh

Shipped: `culler/folder_watcher.py` polls each loaded tab (1.5 s interval, 0.5 s settle,
2 s mtime grace, `:69-71`) and reloads the owning tab, with suppression around the app's own
writes to the scanned folder.

Open: rename detection (a rename arrives as remove + add and loses flags), and reload
granularity — `_on_folder_changed` (`gui.py:516`) still triggers a full `_load_directory`.

### 4.5 R5 — memory budgets

Byte budgets with eviction on insert are in place (`image_loader.py:_store_thumb` `:89`,
`_store_full` `:248`, `_evict_full_cache`), `cache_stats()` (`:263`) exposes them for the
counters, and locks plus per-key single-flight (`:79`, `:189`) make the shared instance safe.

Still open: `orientation` cache is unbounded; the grid's `_ctk_img_cache` is unbounded until
rows are virtualized.

### 4.6 R6 — a huge folder must not bloat memory

Decoded pixels are bounded (§4.5). Widgets are not: one widget set per item, six widgets and
~10 ms each, with no `<Configure>`/scroll handler anywhere in `thumbnail_list.py`.

- **Virtualized rows.** Recycle a pool sized to the viewport (~15–25 rows) instead of one
  widget set per item. This is the only change that makes a 10 000-photo folder viable.
- **Priority queue.** Submit the visible range first, then ±1 screen, then lazily as the user
  scrolls, replacing the whole-folder burst (`thumbnail_list.py:_process_next_batch` `:672`).
- **RAW-first within the viewport**, honouring the existing `_batch_raw_requests` split
  instead of submitting both lists at the same priority.
- **Manifest-only scan** above a folder-size threshold.

### 4.7 R7 — smooth navigation, never a black frame

Done: no stale frame (`gui.py:1327`).

Open: the progressive ladder (400 px proxy → ≤2560 px preview → full decode), an image
pyramid so zoom and pan are blits rather than full resizes, and one bounded prefetch task
replacing the per-navigation thread (`gui.py:1339`), decoding only the primary path.

---

## 5. Concurrency model

- **Threads, not processes, for decode** — RawPy and Pillow release the GIL for the heavy
  parts; processes would add buffer-copy cost.
- **One pool, tier-ordered** so speculative work cannot starve the visible request.
- **ExifTool stays a subprocess**, with chunked argv and 8 concurrent processes.
  `-stay_open` is worth benchmarking: spawn cost is ~150 ms against a 1.05 s metadata pass,
  so the ceiling is ~10 %.
- **Thread safety**: every cache is behind one `RLock` held only for dict mutation, with
  per-key single-flight so N concurrent requests for one path decode once. Duplicate
  submissions are also suppressed at the request layer by path (`thumbnail_list.py:869`).
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
| EXIF rating write | 1 process per rating value (`exif_wrapper.py:572`) | 24 files: 12.22 s → 5.24 s (2.33x) |

Because decoding is I/O bound, reading ~1 MB instead of ~68 MB per ARW mattered more than
any amount of extra threading.

---

## 6. Observability

`cache_stats()` (`image_loader.py:263`) reports thumb/full item counts, resident bytes and
budgets; `ImageLoader.stats` counts thumb/full hits, misses and in-flight waits. These feed
the `Folder: x.xs | Thumbs: x.xs` line and are what the P2 performance test should assert on
rather than wall-clock alone.

Still to wire: surfacing those counters in the UI, and per-phase scan timings (stat, manifest
diff, EXIF, DB, grid build).

---

## 7. Success metrics

| Metric | Measured now | Target |
|---|---|---|
| Folder load, 105-photo ARW folder | **1.40 s** (95 % ExifTool) | < 0.5 s once unchanged files skip EXIF |
| Cold ARW thumbnail | **31 ms** 1 thread, **7 ms** 4 threads | keep |
| Tab switch, loaded 203-photo tab | **16 ms** | < 120 ms (already met) |
| Rapid switching during a load | **119 ms** worst | < 50 ms (needs P2) |
| Longest UI stall, any tab operation | **119 ms** | < 50 ms (needs P2) |
| Soft refresh of the same tab | **4–5 ms** | keep |
| EXIF rating sync, 24 files | **5.24 s** | keep |
| Stale frame after rapid navigation | **0** (`gui.py:1327`) | keep 0 |
| Black frame on navigation | **0** (viewer aspect-fits everything) | keep 0 |
| Thumbnail residency, 3 tabs × 503 photos | **161 MB / 192 MB, 0 evicted** | flat in folder count |
| RSS | bounded by the §4.5 budgets | flat in tab count |

---

## 8. Phased plan

### P0 — done

1. Memoize `ExifToolWrapper.is_available()` (`exif_wrapper.py:73`).
2. Close the stale-preview race (`gui.py:1327`).
3. Byte-budget `_full_cache` (`image_loader.py:67`, `_evict_full_cache`).
4. Never store a full-resolution buffer in the thumbnail tier.
5. Cache ARW preview bytes per `(path, mtime, size)`.
6. Drop the path-only `_thumb_cache_index`; key by `(path, scale, white_balance)`.
7. Locks + per-key single-flight (`image_loader.py:79`).
8. Memoize `load_manual_annotations` by `(path, mtime_ns)`.
9. Chunk ExifTool argv at 200 paths and run 8 chunks concurrently (`exif_wrapper.py:33-43`).
10. Streaming ARW preview extraction with whole-file fallback (`exif_wrapper.py:248`).
11. Import `log_error` in `culler_engine.py` (was a `NameError` on trash failures).
12. Implement `ImageLoader.is_raw()` (`image_loader.py:111`).
13. `save_image_records` batch writer (`db_manager.py:302`).
14. Progress throttle, one update per percent (`culler_engine.py:22`).
15. Batched EXIF rating writes, one process per rating value (`exif_wrapper.py:572`).
16. Fix the tab-switch crash: `_batch_*_requests` were locals, not attributes
    (`thumbnail_list.py:__init__`).
17. Coalesce thumbnail application into one drain tick (`thumbnail_list.py:899`).
18. Time-budgeted row batches (`thumbnail_list.py:26`).
19. Row render cache + path-keyed in-flight de-duplication (`thumbnail_list.py:55`, `:869`).
20. Cancel pending batch chains in both `update_items` paths (`thumbnail_list.py:507`).
21. Base thumbnail completion on outstanding work, not counters, so the duration timer
    actually stops (`thumbnail_list.py:841`).

### P1 — next

22. `FolderIndex` manifest + `apply(change)`; wire the watcher to it instead of
    `_load_directory`; rename detection. **Highest value**: ExifTool is ~95 % of what
    remains of a folder load, and all of a one-file-change reload.
23. Content-identity cache keys (`mtime_ns`, `size`, `normcase` path).
24. Single bounded prefetch task replacing `gui.py:1339`; primary path only.
25. WAL for SQLite.

### P2 — huge folders

26. Scroll handler + recycled row pool; manifest-only scan above a size threshold.
27. Priority thumbnail queue keyed by viewport distance, RAW-first.
28. Move `get_filtered_items` off the UI thread.

### P3 — decode quality

29. Pyramid for the current image; zoom/pan as blits; smooth the proxy→full transition. The
    overlay math in `_draw_detection_rect` (`canvas_viewer.py:180`) recomputes `new_w/new_h`
    at `:207-208` **without** the 3500 px cap `redraw` applies at `:272-276` — that asymmetry
    is the box drift, and it must go through a shared helper (D20).
30. Optional `watchdog` observer as a lower-latency R4 trigger, polling as fallback.

---

## 9. Risks and trade-offs

- **Virtualized rows** change selection, keyboard navigation and multi-select behaviour.
  Highest-risk item; keep the current chunked build behind a setting until proven.
- **Byte budgets** can thrash on rapid zoom/scale changes. Pin the current image and its
  immediate neighbours against eviction.
- **Content-identity keys** mean a file touched externally invalidates its caches — correct,
  but the folder watcher will then evict caches for externally modified files.
- **One shared cache** couples tabs: heavy use of one tab can evict another's thumbnails.
  Mitigate with a small per-tab reservation for the visible range.
- **`mtime_ns` granularity**: filesystems with 1–2 s timestamp resolution can miss a
  same-second modification with identical size. Fall back to a content hash for small files
  when `(mtime_ns, size)` is unchanged but the watcher flagged them.
- **`exiftool -stay_open`** may beat per-batch spawns but adds a long-lived child process to
  manage on shutdown; measure before adopting.

---

## 10. Test strategy

Per `.agents/AGENTS.md`, cover the complicated parts:

- **Unit**: byte accounting and cross-tier eviction; single-flight and path-keyed
  de-duplication; thumbnail keys across scale/WB; manifest diff (add/modify/delete/rename,
  case-only rename, unchanged); prefetch cancellation; row pool recycling.
- **Regression guards** for every defect in §11 — each has a test that fails without the fix.
  These earned their keep: the tab-switch crash and the runaway timer were both found by
  probes rather than review.
- **Tab-switch scenarios** (`tests/test_tab_switch_scenarios.py`): repeated soft refresh,
  switch away and back mid-load, stale results from a superseded load, in-flight
  de-duplication, coalescing, budget yielding, unchanged-row skipping.
- **Integration**: a temp folder with 2000 files asserting a differential refresh does not
  call `clear_cache` or `get_batch_metadata` for unchanged files, and that a tab switch back
  reuses cached thumbnails.
- **Performance smoke test** (opt-in, slow): time to first paint and grid-interactive for
  200/2000/10000 files with RSS sampled, asserting the §7 targets via `cache_stats()`
  rather than wall-clock alone.

---

## 11. Defects

Fixed unless marked open.

| # | Defect | Location |
|---|---|---|
| D1 | Stale fast-preview could overwrite a newer image | `gui.py:1327` — **fixed** |
| D2 | `is_available()` spawned `exiftool -ver` per ARW decode | `exif_wrapper.py:73` — **fixed** |
| D3 | Full-resolution image stored in the thumbnail cache for PNG/HEIC/RAW | `image_loader.py:get_thumbnail` — **fixed** |
| D4 | Thumbnail index keyed by path only; ignored scale/WB | index removed — **fixed** |
| D5 | `_full_cache` item-counted → up to 2.16 GB | `image_loader.py:67` — **fixed** (512 MB) |
| D6 | `_orientation_cache` unbounded, never evicted | `exif_wrapper.py:60` — **open** |
| D7 | No locks while 4+ threads mutated shared caches | `image_loader.py:79` — **fixed** |
| D8 | `annotations.json` re-read and re-resolved every scan | `dataset_exporter.py` — **fixed** |
| D9 | Unchunked ExifTool argv degraded to per-file PIL fallback | `exif_wrapper.py:33` — **fixed** |
| D10 | One thread per navigation and per prefetch, no cap | `gui.py:1207`, `:1339` — **open** (P1-24) |
| D11 | Per-item UI progress callback | `culler_engine.py:22` — **fixed** |
| D12 | Per-tab `ImageLoader`/`ExifToolWrapper` duplicated all caches | `gui.py:822` — **fixed** |
| D13 | Connection + commit per `save_image_record` | `db_manager.py:302` — **fixed** (WAL open) |
| D14 | Every redraw resizes from the source image; no pyramid | `canvas_viewer.py:279` — **open** |
| D15 | `clear_cache()` on every scan, including watcher reloads | `culler_engine.py:292` — **open** (P1-22) |
| D16 | `log_error` used without importing it (`NameError` on trash failures) | `culler_engine.py` — **fixed** |
| D17 | `ImageLoader.is_raw` referenced but undefined (`AttributeError`) | `image_loader.py:111` — **fixed** |
| D18 | `extract_preview_bytes` read the whole 25–60 MB ARW per decode | `exif_wrapper.py:248` — **fixed** |
| D19 | Prefetch decoded every stacked path of adjacent items | `gui.py:1339` — **open** |
| D20 | Contain-fit math duplicated five times; only `redraw` applies the 3500 px cap | `canvas_viewer.py:180`, `:239`, `:475`, `:670`, `:719` — **open** |
| D21 | `_batch_raw_requests` / `_batch_other_requests` were locals, so every soft refresh raised `AttributeError` inside a Tk callback and killed the batch chain | `thumbnail_list.py:__init__` — **fixed** |
| D22 | Thumbnail completion derived from counters that de-duplicated requests could never satisfy, so the duration timer ran forever | `thumbnail_list.py:841` — **fixed** |
| D23 | Sync wrote EXIF ratings with one `exiftool` process per photo | `exif_wrapper.py:572` — **fixed** |
| D24 | Soft refresh re-queued every in-flight decode on tab switch | `thumbnail_list.py:869` — **fixed** |
| D25 | One `after(0)` per decoded thumbnail | `thumbnail_list.py:899` — **fixed** |