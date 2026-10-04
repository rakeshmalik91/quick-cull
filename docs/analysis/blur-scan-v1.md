# Methods to Filter Out Blurry Images

If you want to automatically remove blurry photos from a collection,
there are several approaches depending on your workflow.

## 1. Variance of Laplacian (Most Common)

This is the most popular and fastest method.

-   Converts the image to grayscale.
-   Applies the Laplacian operator to detect edges.
-   Calculates the variance of the result.
-   Lower variance = fewer edges = blurrier image.

### Pros

-   Extremely fast
-   Easy to implement (OpenCV)
-   Good for obvious blur

### Cons

-   Threshold depends on camera and resolution.
-   Can mistake low-texture scenes (sky, walls) for blur.

### Example

``` python
import cv2

img = cv2.imread("image.jpg", cv2.IMREAD_GRAYSCALE)
score = cv2.Laplacian(img, cv2.CV_64F).var()

print(score)
```

Typical thresholds (must be calibrated):

-   **\< 50** : Very blurry
-   **50--100** : Blurry
-   **100--300** : Acceptable
-   **\> 300** : Sharp

------------------------------------------------------------------------

## 2. Tenengrad (Sobel Gradient)

Uses Sobel gradients instead of Laplacian.

### Pros

-   More stable
-   Better for natural photographs

Often preferred in scientific imaging.

------------------------------------------------------------------------

## 3. Brenner Focus Measure

Computes squared differences between pixels a fixed distance apart.

### Pros

-   Very fast
-   Works well for autofocus applications

------------------------------------------------------------------------

## 4. FFT / Frequency Analysis

Sharp images contain more high-frequency information.

Blur suppresses high frequencies.

Good when images have varying content but requires more tuning.

------------------------------------------------------------------------

## 5. Local Variance

Computes texture variance over small regions.

Useful for distinguishing soft-focus from truly blurred images.

------------------------------------------------------------------------

## 6. BRISQUE / NIQE

No-reference image quality assessment algorithms.

They estimate overall perceptual image quality, including blur.

### Pros

-   More robust

### Cons

-   Slower
-   May penalize noise or compression too

------------------------------------------------------------------------

## 7. Deep Learning Models

CNNs trained specifically for blur detection.

Examples:

-   BlurNet
-   CPBD models
-   MobileNet/EfficientNet classifiers trained on sharp vs. blurry
    images

These work best when your definition of "usable" differs from simple
edge sharpness.

------------------------------------------------------------------------

# For Bird Photography

Since bird photography often suffers from:

-   Motion blur
-   Missed autofocus
-   Atmospheric haze
-   Heat shimmer

A single Laplacian score is often not enough.

A better pipeline is:

1.  Detect the bird (YOLO, RT-DETR, etc.).
2.  Crop to the bird.
3.  Compute sharpness only on the bird crop.
4.  Reject images below a threshold.

This avoids images being scored as "sharp" simply because the background
has lots of detail.

------------------------------------------------------------------------

# Recommended OpenCV Metrics to Combine

  Metric                  Detects            Speed
  ----------------------- ------------------ ------------
  Variance of Laplacian   Defocus            ⭐⭐⭐⭐⭐
  Tenengrad               Edge sharpness     ⭐⭐⭐⭐
  Brenner                 Focus              ⭐⭐⭐⭐⭐
  FFT High Frequency      Motion / Defocus   ⭐⭐⭐
  Local Variance          Texture            ⭐⭐⭐⭐

A weighted combination of Laplacian and Tenengrad is often much more
reliable than using either alone.

------------------------------------------------------------------------

# Recommendation

For large wildlife image datasets:

-   Use **Variance of Laplacian** as a fast first-pass filter.
-   Add **Tenengrad** to reduce false positives.
-   If you're already running object detection or cropping subjects
    before classification, compute these metrics **only on the subject
    crop** rather than the whole image. This produces much better
    results for wildlife photography where the background can be
    misleading.
