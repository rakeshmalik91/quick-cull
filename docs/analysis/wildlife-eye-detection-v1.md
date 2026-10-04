# Wildlife Animal/Bird Eye Detection for Photo Quality Filtering

## Objective

Locate the eye of a bird or animal so the eye/head region can be used to assess photographic sharpness and select the best image from similar or burst photographs.

The main approaches are:

1. Traditional image processing based on eye shape/appearance
2. YOLO eye bounding-box detection
3. YOLO keypoint detection
4. A hybrid of these approaches

The recommended strategy is to start with the simplest method that works reliably on your actual photographs.

## 1. Overall Pipeline

```text
Original photograph
        ↓
YOLO animal/bird detection
        ↓
Animal/bird crop
        ↓
Eye detection
   ┌────┴────┐
   ↓         ↓
Traditional  YOLO /
CV           keypoints
   └────┬────┘
        ↓
Eye/head ROI
        ↓
Sharpness analysis
        ↓
Quality score
```

The first YOLO model detects the animal/bird. Eye detection is a separate problem.

## 2. Traditional Eye Detection

Once YOLO has isolated the subject, traditional computer vision can search for likely eye regions using:

- Dark circular/elliptical regions
- Pupil/iris contrast
- Local contrast
- Edge density
- Circularity/ellipse shape
- Expected eye size
- Expected position on the head

Pipeline:

```text
YOLO bird detection
        ↓
Bird crop
        ↓
Eye candidates
        ↓
Shape / contrast filtering
        ↓
Candidate eye
        ↓
Sharpness measurement
```

### Advantages

- No eye-training dataset required
- Very fast
- Easy to experiment with OpenCV
- Can work well for large, clearly visible eyes

### Disadvantages

Bird eyes vary considerably:

- Owls: large, obvious eyes
- Eagles: small dark eyes with prominent brow
- Ducks: small eyes
- Herons: tiny eyes
- Kingfishers: tiny dark eyes
- Woodpeckers: eyes surrounded by complex feathers

Traditional methods can also confuse shadows, feathers, branches and highlights with eyes.

Therefore, treat traditional CV as **candidate generation**, not as guaranteed eye detection.

## 3. Traditional Eye Candidate Scoring

Do not rely on a rule such as:

```text
dark + circular = eye
```

Instead, score candidates using multiple signals:

```text
Eye candidate score =
    darkness
  + circularity
  + local contrast
  + edge structure
  + expected position
  + expected size
```

Useful OpenCV techniques include:

- Thresholding/local thresholding
- Contour detection
- Hough circles
- Ellipse fitting
- Sobel/Canny edges
- Local contrast analysis

## 4. YOLO Eye Bounding-Box Detection

Train YOLO as a single-class detector:

```text
Class 0 = eye
```

Annotate each visible eye with a bounding box.

### Advantages

- Learns complex eye appearance
- Handles feathers/backgrounds better than simple thresholding
- Provides confidence scores
- Easy to integrate into a YOLO pipeline

### Disadvantages

- Requires annotated training data
- Small eyes are difficult
- Requires training and validation
- May still miss tiny or heavily occluded eyes

This is the simplest neural-network eye detector to implement.

## 5. YOLO Keypoint Detection

Instead of an eye bounding box, train a keypoint model.

For example:

```text
Keypoint 0 = left eye
Keypoint 1 = right eye
```

The model predicts the exact eye location.

### Advantages

- Precise eye location
- Excellent for extracting an eye-centered ROI
- Can be extended to other anatomical landmarks
- Avoids unnecessarily large bounding boxes

### Disadvantages

- More complicated annotation
- Requires keypoint training
- Tiny/occluded eyes remain difficult

For a mature photographic-quality system, keypoints may be preferable.

## 6. Comparison

| Method | Training | Accuracy Potential | Speed | Complexity |
|---|---:|---:|---:|---:|
| Traditional CV | No | Low–Medium | Very high | Low |
| YOLO eye box | Yes | High | High | Medium |
| YOLO keypoint | Yes | Very high | High | Medium–High |
| Hybrid CV + YOLO bird | No eye training | Medium–High | Very high | Medium |
| YOLO + eye model + CV | Yes | Very high | High | High |

## 7. Recommended Development Strategy

### Phase 1 — Traditional CV Prototype

Start with:

```text
YOLO bird detector
        ↓
Bird crop
        ↓
Traditional eye candidate detection
        ↓
Sharpness scoring
```

Test this on several hundred of your real photographs.

### Phase 2 — Evaluate Failures

Record images where traditional detection fails:

- Tiny distant eyes
- Eyes hidden by vegetation
- Side profiles
- Dark eyes against dark feathers
- Strong reflections
- Closed eyes
- Backlighting
- Unusual species
- False detections on feathers

These failures are valuable training examples.

### Phase 3 — Train YOLO Eye Detector

If traditional detection is insufficient:

```text
Your wildlife photos
        ↓
Annotate eyes
        ↓
Train YOLO eye detector
        ↓
Validate
        ↓
Add difficult failures
        ↓
Retrain
```

A few hundred to around 1,000 representative images can be a useful starting point. Diversity matters more than simply maximizing image count.

### Phase 4 — Consider Keypoints

If bounding boxes are not precise enough:

```text
YOLO bird detector
        ↓
YOLO eye keypoint model
        ↓
Exact eye location
        ↓
Eye-centered ROI
        ↓
Sharpness
```

## 8. Tiny Eyes: The Major Problem

Example:

```text
Original: 6000 × 4000
Bird:      800 × 600
Eye:        12 × 12
```

Directly resizing the whole image to a small YOLO input can destroy useful eye information.

Use:

```text
6000 × 4000 original
        ↓
Bird detection
        ↓
Bird crop
        ↓
Upscale / high-resolution processing
        ↓
Eye detection
```

This gives the eye detector more useful pixels.

## 9. Eye ROI for Sharpness

Do not necessarily calculate sharpness on only the exact eye box.

Instead:

```text
Eye detection
     ↓
Expand ROI by roughly 50–100%
     ↓
Sharpness measurement
```

The expanded ROI may include the pupil, iris, eyelids and nearby feathers/fur, providing more texture for the sharpness metrics.

## 10. Sharpness Metrics

Once an eye/head ROI is identified, calculate:

### Variance of Laplacian

```python
score = cv2.Laplacian(
    gray,
    cv2.CV_64F
).var()
```

### Tenengrad

Uses Sobel gradients and can be more stable for natural photographs.

### Local contrast

Useful as an additional quality signal.

A combined score is preferable to relying on one metric.

## 11. Eye Detection Confidence Is Not Sharpness

These are different measurements.

Example:

```text
Eye confidence = 0.96
Sharpness      = 45
```

The detector is confident, but the eye may be badly blurred.

Another image:

```text
Eye confidence = 0.65
Sharpness      = 400
```

The eye may be sharp, but detection is less certain.

Keep both measurements and combine them only after normalization.

## 12. Handling Multiple Eyes

If two eyes are detected:

```text
Left eye  → confidence 0.92 → sharpness 240
Right eye → confidence 0.87 → sharpness 310
```

Possible strategies:

### Maximum

```python
score = max(left_score, right_score)
```

Useful when one eye is visible and the other is obscured.

### Average

```python
score = (left_score + right_score) / 2
```

Useful for frontal portraits.

### Confidence-weighted score

Weight sharpness according to detection confidence.

For wildlife photography, the best-eye strategy is often practical because one eye may naturally be hidden.

## 13. Closed and Occluded Eyes

Define rules during dataset creation.

- **Open eye:** normal detection
- **Partially closed:** detect if sufficiently visible
- **Completely closed:** ignore or create a separate `closed_eye` class
- **Occluded:** annotate if enough of the eye is identifiable

## 14. One Model or Separate Models?

For birds only:

```text
Bird detector
     ↓
Bird-eye detector
```

is a good design.

For many animal groups:

```text
Animal type
    ├── Birds    → bird-eye model
    ├── Mammals  → mammal-eye model
    └── Reptiles → reptile-eye model
```

You can initially use one generic `eye` class. Separate models are only necessary if one model performs poorly across animal types.

## 15. Recommended Final Architecture

```text
                 ORIGINAL PHOTO
                       ↓
                YOLO BIRD DETECTOR
                       ↓
                  BIRD CROP
                       ↓
             HIGH-RESOLUTION CROP
                       ↓
                EYE DETECTION
               /              \
              /                \
     Traditional CV          YOLO eye
     candidate search        detector
              \                /
               \              /
                ↓            ↓
                  Eye candidates
                       ↓
                 Eye ROI selection
                       ↓
              Sharpness measurements
                       ↓
          ┌────────────┼────────────┐
          ↓            ↓            ↓
      Laplacian    Tenengrad    Contrast
          └────────────┼────────────┘
                       ↓
                  Eye quality
                       ↓
                 Photo quality
```

The traditional CV branch can also be retained as a fallback when the neural detector fails.

## 16. Best Practical Recommendation

For bird photography, **do not immediately train a dedicated eye YOLO model**.

Start with:

```text
1. YOLO → detect bird
2. Crop bird
3. Traditional CV → generate eye candidates
4. Measure sharpness around candidates
5. Evaluate results
```

If this is sufficiently reliable, you have a fast solution without additional training.

If it fails on too many real photographs:

```text
6. Annotate eyes in your own photos
7. Train YOLO eye detector
8. Use it instead of, or alongside, traditional CV
```

If you eventually need very precise eye localization:

```text
9. Move from eye bounding boxes to keypoint detection
```

This avoids spending significant time training an eye model before knowing whether it is necessary.

## 17. Complete Photo-Culling Workflow

The eventual system can be:

```text
Photo collection
       ↓
Exact duplicate filtering
       ↓
pHash near-duplicate grouping
       ↓
YOLO bird/animal detection
       ↓
Subject crop
       ↓
Eye detection
       ├── Traditional CV
       ├── YOLO eye detector
       └── YOLO keypoint model
       ↓
Eye/head sharpness
       ↓
Photo quality score
       ↓
Best image from each burst/duplicate group
```

The key principle is that **eye detection is a means to obtain a meaningful sharpness region, not the final objective**. The system should ultimately select the photograph with the best usable subject detail, rather than simply the photograph with the highest eye-detection confidence.
