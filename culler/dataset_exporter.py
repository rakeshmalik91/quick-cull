import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from PIL import Image

from culler.paths import DATASET_DIR

ANNOTATIONS_FILENAME = "annotations.json"

#: (annotations path, mtime_ns) -> parsed annotations. Avoids re-reading and re-resolving
#: every key on each directory scan.
_ANNOTATION_CACHE: Dict[Tuple[str, int], Dict[str, Dict[str, Any]]] = {}


def get_dataset_dir(dataset_dir: Optional[str] = None) -> Path:
    """Returns the base dataset directory Path."""
    return Path(dataset_dir) if dataset_dir else DATASET_DIR


def get_annotations_file(dataset_dir: Optional[str] = None) -> Path:
    """Returns the path to the structured annotations JSON file."""
    return get_dataset_dir(dataset_dir) / ANNOTATIONS_FILENAME


def create_dataset_structure(dataset_dir: Optional[str] = None):
    """Creates the YOLO format dataset directory structure."""
    base = get_dataset_dir(dataset_dir)
    images_dir = base / "images" / "train"
    labels_dir = base / "labels" / "train"
    
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)
    
    yaml_path = base / "dataset.yaml"
    if not yaml_path.exists():
        yaml_content = f"""path: {base.absolute().as_posix()}
train: images/train
val: images/train

names:
  0: subject
  1: eye
"""
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)
            
    return images_dir, labels_dir


def load_manual_annotations(dataset_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """
    Loads all manual bounding box annotations from _DATASET/annotations.json.
    Returns a dict mapping resolved image paths to annotation dicts:
    {
        "path/to/image.jpg": {
            "manual_detection_box": (x1, y1, x2, y2) or None,
            "manual_eye_box": (x1, y1, x2, y2) or None,
            "filename": "image.jpg",
            "updated_at": "..."
        }
    }

    Cached by (path, mtime_ns): this runs on every directory scan, and the resolve()
    per stored key made each scan scale with the size of the annotation file.
    """
    json_path = get_annotations_file(dataset_dir)
    if not json_path.exists():
        return {}

    try:
        stamp = json_path.stat().st_mtime_ns
    except OSError:
        return {}

    cached = _ANNOTATION_CACHE.get((str(json_path), stamp))
    if cached is not None:
        return cached

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

            annotations: Dict[str, Dict[str, Any]] = {}
            for raw_path, record in raw_data.items():
                norm_path = str(Path(raw_path).resolve())
                det_box = None
                if record.get("manual_detection_box"):
                    b = record["manual_detection_box"]
                    if isinstance(b, (list, tuple)) and len(b) == 4:
                        det_box = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))

                eye_box = None
                if record.get("manual_eye_box"):
                    eb = record["manual_eye_box"]
                    if isinstance(eb, (list, tuple)) and len(eb) == 4:
                        eye_box = (float(eb[0]), float(eb[1]), float(eb[2]), float(eb[3]))

                annotations[norm_path] = {
                    "manual_detection_box": det_box,
                    "manual_eye_box": eye_box,
                    "filename": record.get("filename", Path(raw_path).name),
                    "updated_at": record.get("updated_at", "")
                }
            _ANNOTATION_CACHE[(str(json_path), stamp)] = annotations
            return annotations
    except Exception as e:
        print(f"Error loading manual annotations from {json_path}: {e}")
        return {}


def clear_manual_annotation_cache() -> None:
    """Drop the memoised annotations (call after writing annotations.json)."""
    _ANNOTATION_CACHE.clear()


def get_manual_annotation(image_path: str, dataset_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves manual annotation for a specific image path if present."""
    all_annos = load_manual_annotations(dataset_dir)
    norm_path = str(Path(image_path).resolve())
    return all_annos.get(norm_path)


def save_manual_annotation(
    image_path: str,
    manual_detection_box: Optional[Tuple[float, float, float, float]] = None,
    manual_eye_box: Optional[Tuple[float, float, float, float]] = None,
    dataset_dir: Optional[str] = None
) -> bool:
    """
    Saves or updates manual bounding box coordinates (normalized 0.0-1.0)
    for an image into _DATASET/annotations.json.
    """
    try:
        base = get_dataset_dir(dataset_dir)
        base.mkdir(parents=True, exist_ok=True)
        json_path = get_annotations_file(dataset_dir)
        
        # Load existing
        raw_data: Dict[str, Any] = {}
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
            except Exception:
                raw_data = {}
                
        norm_path = str(Path(image_path).resolve())
        
        if manual_detection_box is None and manual_eye_box is None:
            # If both are None, remove entry if present
            raw_data.pop(norm_path, None)
        else:
            raw_data[norm_path] = {
                "filename": Path(image_path).name,
                "manual_detection_box": list(manual_detection_box) if manual_detection_box else None,
                "manual_eye_box": list(manual_eye_box) if manual_eye_box else None,
                "updated_at": datetime.now().isoformat()
            }
            
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, indent=2)

        _ANNOTATION_CACHE.clear()
        return True
    except Exception as e:
        print(f"Error saving manual annotation to {dataset_dir}: {e}")
        return False


def delete_manual_annotation(image_path: str, dataset_dir: Optional[str] = None) -> bool:
    """Deletes the manual annotation entry for an image from _DATASET/annotations.json."""
    try:
        json_path = get_annotations_file(dataset_dir)
        if not json_path.exists():
            return True
            
        with open(json_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
            
        norm_path = str(Path(image_path).resolve())
        if norm_path in raw_data:
            del raw_data[norm_path]
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(raw_data, f, indent=2)
            _ANNOTATION_CACHE.clear()
        return True
    except Exception as e:
        print(f"Error deleting manual annotation: {e}")
        return False


def normalize_box(box: Tuple[int, int, int, int], img_w: int, img_h: int) -> str:
    """Converts (x1, y1, x2, y2) to YOLO format (x_center, y_center, width, height) normalized."""
    x1, y1, x2, y2 = box
    
    # Ensure bounds
    x1 = max(0, min(x1, img_w))
    x2 = max(0, min(x2, img_w))
    y1 = max(0, min(y1, img_h))
    y2 = max(0, min(y2, img_h))
    
    bw = x2 - x1
    bh = y2 - y1
    cx = x1 + bw / 2.0
    cy = y1 + bh / 2.0
    
    # Normalize to 0.0 - 1.0
    cx_n = cx / img_w
    cy_n = cy / img_h
    bw_n = bw / img_w
    bh_n = bh / img_h
    
    return f"{cx_n:.6f} {cy_n:.6f} {bw_n:.6f} {bh_n:.6f}"


def save_annotation(
    image_path: str,
    img_w: int,
    img_h: int,
    subject_box: Optional[Tuple[int, int, int, int]],
    eye_box: Optional[Tuple[int, int, int, int]],
    dataset_dir: Optional[str] = None,
    pil_image=None
) -> bool:
    """
    Saves an image and its annotations into a YOLO training dataset under _DATASET/,
    and records the structured manual bounding boxes in _DATASET/annotations.json.
    Returns True on success.
    """
    if not subject_box and not eye_box:
        return False
        
    try:
        images_dir, labels_dir = create_dataset_structure(dataset_dir)
        
        # Generate unique ID for this sample
        sample_id = str(uuid.uuid4())[:8]
        orig_name = Path(image_path).name
        safe_name = f"anno_{sample_id}_{orig_name}"
        if not safe_name.lower().endswith(('.jpg', '.jpeg', '.png')):
            safe_name += ".jpg"
            
        target_img_path = images_dir / safe_name
        target_lbl_path = labels_dir / f"{Path(safe_name).stem}.txt"
        
        # Save image (resize to max 1920px for fast decoding, RAM caching, & minimal disk footprint)
        if pil_image:
            img_to_save = pil_image.copy()
            if hasattr(img_to_save, "mode") and img_to_save.mode in ("RGBA", "P"):
                img_to_save = img_to_save.convert("RGB")
            img_to_save.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            img_to_save.save(target_img_path, format="JPEG", quality=90)
        else:
            try:
                with Image.open(image_path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                    im.save(target_img_path, format="JPEG", quality=90)
            except Exception:
                shutil.copy2(image_path, target_img_path)
        
        # Write YOLO labels
        lines = []
        norm_subj = None
        norm_eye = None
        
        if subject_box:
            box_str = normalize_box(subject_box, img_w, img_h)
            lines.append(f"0 {box_str}")
            sx1, sy1, sx2, sy2 = subject_box
            norm_subj = (
                max(0.0, min(1.0, sx1 / img_w)),
                max(0.0, min(1.0, sy1 / img_h)),
                max(0.0, min(1.0, sx2 / img_w)),
                max(0.0, min(1.0, sy2 / img_h))
            )
        
        if eye_box:
            box_str = normalize_box(eye_box, img_w, img_h)
            lines.append(f"1 {box_str}")
            ex1, ey1, ex2, ey2 = eye_box
            norm_eye = (
                max(0.0, min(1.0, ex1 / img_w)),
                max(0.0, min(1.0, ey1 / img_h)),
                max(0.0, min(1.0, ex2 / img_w)),
                max(0.0, min(1.0, ey2 / img_h))
            )
            
        with open(target_lbl_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            
        # Also persist structured annotation in annotations.json under _DATASET
        save_manual_annotation(
            image_path=image_path,
            manual_detection_box=norm_subj,
            manual_eye_box=norm_eye,
            dataset_dir=dataset_dir
        )
            
        return True
    except Exception as e:
        print(f"Error saving annotation: {e}")
        return False
