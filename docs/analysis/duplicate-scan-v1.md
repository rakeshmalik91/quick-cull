# Duplicate Image Detection Using Similarity Thresholds

## Overview

When you want to treat images as **identical within a threshold**, the
standard approach is to use **Perceptual Hashing (pHash)** rather than
comparing file bytes.

Instead of checking whether two files are exactly the same, pHash
compares their visual appearance.

------------------------------------------------------------------------

## pHash + Hamming Distance

1.  Compute a perceptual hash (typically 64 bits) for each image.
2.  Compare the hashes using **Hamming Distance** (the number of
    differing bits).
3.  If the distance is below a chosen threshold, treat the images as
    duplicates.

### Example

``` python
from PIL import Image
import imagehash

hash1 = imagehash.phash(Image.open("img1.jpg"))
hash2 = imagehash.phash(Image.open("img2.jpg"))

distance = hash1 - hash2

if distance <= 8:
    print("Duplicate")
```

------------------------------------------------------------------------

## Recommended Thresholds

  -----------------------------------------------------------------------
                       Hamming Distance Interpretation
  ------------------------------------- ---------------------------------
                                      0 Exact visual match

                                   1--4 Nearly identical (JPEG
                                        compression, metadata changes)

                                   5--8 Same image with minor edits
                                        (resize, slight color/exposure
                                        changes)

                                  9--12 Possibly the same image; review
                                        manually

                                   \>12 Usually different images
  -----------------------------------------------------------------------

For wildlife photography, a threshold of **6--8** is a good starting
point.

------------------------------------------------------------------------

# Burst Photography

Perceptual hashes may fail to group burst shots because even small
subject movement changes many hash bits.

For burst images, deep-learning embeddings work much better.

Recommended models:

-   CLIP
-   DINOv2

These convert each image into a feature vector and compare them using
cosine similarity.

Example similarity scores:

    Cosine Similarity Interpretation
  ------------------- ----------------------------------------
                 1.00 Same image
               \>0.99 Duplicate
           0.97--0.99 Near duplicate / burst shot
           0.94--0.97 Same bird or scene with different pose
               \<0.90 Different images

Typical threshold:

-   **0.97--0.98** for burst-shot grouping.

------------------------------------------------------------------------

# Recommended Workflow

For a large wildlife photo collection:

1.  Remove exact duplicate files using **MD5** or **SHA-256**.
2.  Compute **pHash** for all remaining images.
3.  Group images with a Hamming distance of **6--8**.
4.  Compute blur and quality scores within each group.
5.  Keep the highest-quality image.
6.  Optionally, use **CLIP** or **DINOv2** embeddings with cosine
    similarity **0.97--0.98** to merge burst shots that pHash misses.

------------------------------------------------------------------------

# Summary

  -----------------------------------------------------------------------
  Method              Threshold                  Best Use
  ------------------- -------------------------- ------------------------
  MD5 / SHA-256       Exact match                Identical files

  pHash               Hamming distance ≤ 6--8    Near-identical images

  CLIP / DINOv2       Cosine similarity ≥ 0.97   Burst shots and visually
                                                 similar photos
  -----------------------------------------------------------------------

## Recommendation

For most wildlife photography collections:

-   Use **pHash (threshold 6--8)** for fast duplicate removal.
-   Use **CLIP or DINOv2 (cosine similarity 0.97--0.98)** to identify
    burst shots and highly similar images that are not pixel-identical.
