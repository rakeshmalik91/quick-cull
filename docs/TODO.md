# TODO list
---------------------
Add latest changes at top
Do not add any new details here. only use this for checking/unchecking the TODO list.
Make corrections optionally, if required.
---------------------

- [ ] UI changes:
  - [ ] add a bit of gap between "Close all Tabs" & "About" button
  - [ ] add hide/unhide for thumbnail panel and right side tool panel
  - [ ] make each sections of right side tool panel collapsible: "Culling Actions", "Exif Metadata"
  - [ ] save state of the UI (panel positions, sizes, hide/unhide, panel sections collapsed/expanded, menubar state) for relaunch
  - [ ] 
- [ ] Add a search by filename, to jump to a file in the thumbnails list. it should suggest continuously as typed. Add it right on top of thumbnail list. Ctrl+F to focus on it.
- [ ] Scrolling on thumbnails with large data: glitch, freeze & performance
  - [x] some times thumbnails show blanks, and selecting a thumbnail shows the image in preview but doesnt highlight with blue in thumbnails list
  - [ ] when scrolling fast, text from new thumbnails overlap with old ones, making it look glitchy (last try on this reduced the glitch and increased freeze. need to accept completely removed glitch/freeze)
- [ ] virtualize the thumbnail grid rows (recycled row pool + scroll-driven rebinding). needed for folders above ~1500 photos: 2771 rows currently take ~111s to build and freeze the app. see docs/analysis/ui-framework-assessment.md
- [x] if there is only one tab open, not able to close it. ensure closing a tab clears the memory completely. add a close all tabs button too. 
- [x] tab button and tab close buttons are glitching all the time. thumbnail list glitching during folder/thumbnail loading. verify our ui framework is correctly being used or requires any replacement.
- [x] create a doc for improving the folder and thumbnail load time
    - it should happen in background, shouldnt block ui. goal is to load images fastest possible without any glitches or freezing
    - moving between multiple tabs should not load same stuffs multiple times, should be cached
    - on manual or on trigger refresh, diffeantial refresh instead of full refresh
    - auto refresh if files get added/deleted/renamed/updated/etc from some other applications
    - caching shouldnt bloat memory if there are too many images
    - loading a folder shouldnt also bloat memory if there are too many images in single folder
    - navigating between images should be smooth and fast, regardless of image is fully/partially loaded. during navigation the image should be visible on screen at scale it has been loaded at that point of time, not with black screen
    - consider using multithreading and gpu for faster operations
    - consider using exiftool batch processing if available
      (see docs/analysis/load-performance-v1.md)
- [x] mark keeper tag as "Duplicate" too by default, not "Keeper", make stars configurable for keeper too
- [x] remove the custom tag button on right panel, in settings create a new tab to manage tags
- [x] for duplicate add feature for keeper selection methods. 
- [x] add a keeper selection method specific for bird/wildlife, based on eye sharpness
- [x] add support for multiselect in tag & rating filter
- [x] add refresh button beside open folder (use icon, no text)
- [x] add support for multiple tabs. add new tab or open new folder should add a new tab. remove existing open folder button. should be able to reorder tabs. if multiple tabs are open, on releaunch should restore all tabs. on relaunch, current active tab should be show all data, other tabs data should be lazy loaded. once loaded other moving to other tabs should be seemless, should not trigger lazy load again. each tab should have its own independent filter settings. 
- [x] while loading thumbnails, load RAW first then JPG or anything else, they take long and block RAW thumbnails
- [x] add an option for safe blur scan, it will check for loose duplicates, and mark for reject only if another non blurry or less blurry version of the image is present