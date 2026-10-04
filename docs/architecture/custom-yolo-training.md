# Custom Training YOLO for Photo Culling

Fine-tuning or custom-training YOLO models on dedicated bird, animal, and insect datasets dramatically improves detection precision, eye-tracking accuracy, and blur culling performance in the Image Culler application.

## 🎯 1. Limitations of Pre-Trained Models

Currently, the pipeline uses generic COCO pre-trained models (`yolov8n.pt` and `yolov8n-pose.pt`):

| Aspect | COCO Pre-trained Model (`yolov8n.pt`) | Custom Fine-Tuned Model |
|---|---|---|
| **Classes** | 80 broad categories (`person`, `bird`, `dog`, `cat`...) | Targeted ROIs (`subject_body`, `head_eye`, `insect`, `macro_eye`) |
| **Insects & Macro** | ❌ **No insect class** in COCO (misses bees, butterflies, dragonflies) | ✅ **Native Insect Detection** (detects compound eyes, antennae, wings) |
| **Eye Detection** | Relies on OpenCV DoG morphology or human pose keypoints | ✅ **Direct Eye Bounding Boxes** learned directly from annotation data |
| **Camouflage & Foliage** | Struggles with songbirds behind leaves or dark plumage in shade | ✅ **High Recall** trained on wildlife datasets in natural habitats |
| **Bounding Box Precision** | Loose boxes often including tail feathers, branches, or background | ✅ **Tight Inset Boxes** focused on high-importance sharpness areas |

---

## 🚀 2. Specific Benefits for Photo Culling

### A. Macro & Insect Photography Support
In macro photography (where depth-of-field is often < 1mm), the generic COCO model ignores insects because COCO contains no insect class. Fine-tuning adds an `insect` and `insect_eye` class, allowing the culler to evaluate micro-sharpness on compound eyes or antennae automatically.

### B. Single-Pass 2-Level Detection (Level 1 Subject + Level 2 Eye)
Instead of running **YOLO -> Morphological DoG -> Pose Keypoints -> Saliency**, a fine-tuned 2-class YOLO model (Class 0: `subject`, Class 1: `eye`) returns **both Level 1 (Body) and Level 2 (Eye) bounding boxes in a single forward pass (~10ms)**.

### C. Bird Species & Shadow Camouflage
Wildlife shots often feature birds perched deep inside dense foliage or under dark shadows. Fine-tuning on wildlife datasets eliminates false negatives caused by broken silhouettes or dappled light.

---

## 📚 3. Recommended Datasets for Fine-Tuning

1. **Bird Datasets**:
   - **[CUB-200-2011](http://www.vision.caltech.edu/visipedia/CUB-200-2011.html)**: 11,788 bird images with bounding boxes and 15 keypoint annotations (eyes, beak, head).
   - **[NABirds](https://dl.allaboutbirds.org/nabirds)**: 48,000+ annotated North American bird photos across 400+ species.
2. **Insect & Macro Datasets**:
   - **[IP102](https://github.com/xp2600/IP102)**: Large-scale insect dataset with 75,000+ images.
   - **[iNaturalist (iNat2021)](https://github.com/visipedia/inat_comp/tree/master/2021)**: Millions of high-quality wildlife, insect, bird, and reptile photos.
3. **Custom Culling Dataset**:
   - Annotate 500–1,000 representative wildlife/macro photos using [Roboflow](https://roboflow.com/) or [Label Studio](https://labelstud.io/) with two labels: `subject` and `eye`.

---

## 🛠️ 4. Recommended Fine-Tuning Pipeline

### Step 1: Train YOLOv8 Nano or Small
```python
from ultralytics import YOLO

# Load base YOLOv8 model
model = YOLO("yolov8n.pt")

# Train on custom wildlife dataset
results = model.train(
    data="wildlife_culling.yaml",  # classes: ['subject', 'eye']
    epochs=100,
    imgsz=640,
    batch=16,
    device=0
)
```

### Step 2: Export to ONNX for High-Speed CPU Inference
Exporting to **ONNX Format** allows PyTorch-free, ultra-fast CPU inference inside the Python application:
```python
# Export model to ONNX for lightweight CPU deployment
model.export(format="onnx", dynamic=True, simplify=True)
```

---

## 💡 Summary Recommendation

- **For General Photography (Portraits, Events, Studio)**: The current pipeline (YOLOv8 + Pose + Laplacian) works very well.
- **For Wildlife, Birding & Macro Photography**: **Fine-tuning a custom YOLOv8 model on bird/insect eye data is the single best upgrade** to achieve sub-millimeter eye focus culling and 100% macro support.
