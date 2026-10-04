# TODO list
---------------------
Add latest changes at top
Do not add any new details here. only use this for checking/unchecking the TODO list.
Make corrections optionally, if required.
---------------------

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
      (see docs/load-performance-v1.md)
- [x] mark keeper tag as "Duplicate" too by default, not "Keeper", make stars configurable for keeper too
- [x] remove the custom tag button on right panel, in settings create a new tab to manage tags
- [x] for duplicate add feature for keeper selection methods. 
- [x] add a keeper selection method specific for bird/wildlife, based on eye sharpness
- [x] add support for multiselect in tag & rating filter
- [x] add refresh button beside open folder (use icon, no text)
- [x] add support for multiple tabs. add new tab or open new folder should add a new tab. remove existing open folder button. should be able to reorder tabs. if multiple tabs are open, on releaunch should restore all tabs. on relaunch, current active tab should be show all data, other tabs data should be lazy loaded. once loaded other moving to other tabs should be seemless, should not trigger lazy load again. each tab should have its own independent filter settings. 
- [x] while loading thumbnails, load RAW first then JPG or anything else, they take long and block RAW thumbnails
- [x] add an option for safe blur scan, it will check for loose duplicates, and mark for reject only if another non blurry or less blurry version of the image is present