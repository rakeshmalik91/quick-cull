# Scan for Duplicate (Perceptual Hashing & Duplicate Detection Pipeline)

This document details the approach for detecting duplicate, near-identical, or burst shot photos during photographic culling sessions, picking the sharpest shot as a `PICK`, and tagging/flagging all redundant duplicates as `REJECT`.

---

## 🔬 Duplicate Detection Methods & Similarity Thresholds

| Method | Threshold / Metric | Best Use Case |
| :--- | :--- | :--- |
| **MD5 / SHA-256** | Exact byte match | Identical duplicate files |
| **dHash / pHash** | Hamming Distance $\le 6 - 8$ bits | Near-identical photos, JPEG re-compression, slight crop/exposure edits |
| **Burst Camera Sequence** | Time $\le 2.0\text{s}$ & Stem Match | Burst sequence shot grouping |
| **Deep Embeddings (CLIP / DINOv2)** | Cosine Similarity $\ge 0.97 - 0.98$ | Complex burst shots with subject pose movement |

---

## 📊 Recommended Hamming Distance Thresholds (pHash / dHash)

| Hamming Distance | Visual Similarity Interpretation |
| :---: | :--- |
| **0** | Exact visual match |
| **1 – 4** | Nearly identical (JPEG compression, metadata change) |
| **5 – 8** | Same image with minor edits (crop, color/exposure tweak). **Recommended for Wildlife/Burst photography (6–8)** |
| **9 – 12** | Possibly same image (manual review suggested) |
| **> 12** | Distinct, different images |

---

## ⚡ 64-Bit Difference Hash (dHash) & Hamming Distance

To detect visually identical photos despite small exposure variations or framing shifts:
1. Downsample image to $9 \times 8$ pixels in grayscale.
2. Compare luminance values between adjacent columns ($8 \times 8 = 64$ comparisons).
3. Construct a 64-bit binary integer hash:

$$H_i = \begin{cases} 1 & \text{if } P_{r,c} > P_{r,c+1} \\ 0 & \text{otherwise} \end{cases}$$

4. **Hamming Distance Clustering**: The difference between two 64-bit perceptual hashes is measured using XOR bitwise Hamming distance:

$$\text{Distance}(H_1, H_2) = \text{popcount}(H_1 \oplus H_2)$$

- A Hamming distance $\le 6$ indicates photos are visually identical or part of the same burst sequence.

---

## 🏆 Sharpness-Based Keeper Selection Contract

For every cluster of duplicate photos:
1. Calculate edge sharpness scores across all candidates in the group.
2. Select the candidate with the highest sharpness score as the **Keeper** and set its flag to `PICK`.
3. Set all remaining duplicate photos in the group to `REJECT` and apply the `"Duplicate"` tag to `item.tags`.
4. Save updated flags, ratings, and tags to SQLite database `image_records`.

---

## 💻 Code Reference

```python
# culler/culler_engine.py - CullingSession._compute_dhash & scan_for_duplicates
def _compute_dhash(self, img: Image.Image) -> int:
    small = img.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = list(small.getdata())
    diff = [pixels[r * 9 + c] > pixels[r * 9 + c + 1] for r in range(8) for c in range(8)]
    val = 0
    for b in diff:
        val = (val << 1) | b
    return val

def scan_for_duplicates(self, threshold: int = 6) -> List[ImageItem]:
    # Group items by dHash Hamming distance <= threshold
    # Sort groups by sharpness score
    # Keeper -> PICK, Duplicates -> REJECT + Tag 'Duplicate'
```
