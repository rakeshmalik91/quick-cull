# Active Learning & Incremental Training Pipeline

This document outlines the design for the interactive YOLO annotation and incremental custom training feature in Image Culler.

## Overview
Image Culler now operates as a complete Active Learning platform. It allows you to automatically correct the AI when it makes a mistake (like failing to detect a camouflaged bird or small insect), build a custom dataset organically during your culling workflow, and fine-tune the model to your specific photography domain.

## Architecture

```mermaid
flowchart TD
    A[Image Culler (Scanner)] -->|Misses Subject| B[User Enters Annotation Mode]
    B -->|Draws Subject & Eye Boxes| C[culler/dataset_exporter.py]
    C -->|Normalizes Coordinates| D[Saves to _DATASET]
    D --> E[User Triggers Custom Training]
    E --> F[culler/ml_trainer.py (ultralytics)]
    F -->|Fine-tunes yolov8n.pt on background thread| G[Generates best.pt]
    G -->|Copies to| H[lib/models/yolo_custom.pt]
    H -->|Hot-Reloads into| I[culler/detectors/blur/yolo_subject.py]
    I -->|Next Scan Uses Custom Model| A
```

## Features

### 1. Interactive Annotation UI (`culler/gui/canvas_viewer.py`)
When you click **`🎯 Annotate YOLO (Train)`**, the UI switches to Annotation Mode.
- A new toolbar lets you select the class to draw: **Subject (Green)** or **Eye (Gold)**.
- Left-click and drag creates precise bounding boxes around your target.
- The UI translates these drawn pixel coordinates back to the original image dimensions.

### 2. Dataset Exporter (`culler/dataset_exporter.py`)
When you click save, the `dataset_exporter` module handles the YOLO format generation:
- Copies the original image to `_DATASET/images/train/`.
- Converts the `(x1, y1, x2, y2)` pixel coordinates into YOLO normalized format `(class_id, center_x, center_y, width, height)` where all values are floats between 0.0 and 1.0.
- Writes the labels to `_DATASET/labels/train/`.
- Automatically generates a `dataset.yaml` with the classes (`subject`, `eye`).

### 3. Background ML Trainer (`culler/ml_trainer.py`)
Runs the `ultralytics` training pipeline seamlessly:
- Starts a background thread with `workers=0` (essential for Windows stability).
- Fine-tunes the `yolov8n.pt` base model using your newly annotated data.
- Once training completes (default 25 epochs), it automatically locates `runs/culler_custom/weights/best.pt`.
- Copies the fine-tuned weights to the application's internal library `lib/models/yolo_custom.pt`.

### 4. Hot-Reloading Inference Router (`culler/detectors/blur/yolo_subject.py`)
The AI scanner is designed to dynamically upgrade itself.
- On initialization, it checks for `lib/models/yolo_custom.pt`.
- If found, it intercepts the standard model loading procedure and loads your custom weights instead.
- This creates a continuous feedback loop where the application gets smarter the more you use it.

## Example

![Annotation Mode](/d:/Projects/image-culler/media/ss1.jpg)
