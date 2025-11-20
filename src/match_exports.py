import os
import json
from datetime import datetime
import exifread
import re

# --- CONFIG ---
BASE_DIR = "/Volumes/Docker/Media/Photo Archive"
BACKUPS_DIR = "./backups"

# Ensure BACKUPS_DIR is an absolute path
BACKUPS_DIR = os.path.abspath(BACKUPS_DIR)

# Find the latest backup file dynamically
backup_files = [f for f in os.listdir(BACKUPS_DIR) if f.startswith("lightroom_backup_") and f.endswith("_local.json")]
if not backup_files:
    raise FileNotFoundError("No backup files found in the backups directory.", BACKUPS_DIR)

latest_backup = max(backup_files, key=lambda f: f)
JSON_PATH = os.path.join(BACKUPS_DIR, latest_backup)
# -------------


# ---------- Utilities ----------

def normalize(rel):
    """Normalize relative file paths for consistent comparison."""
    return rel.strip("/").replace("\\", "/").lower()


def scan_local_files(base_dir):
    """Return rel_path -> abs_path dict, ignoring .DS_Store."""
    local = {}
    base_len = len(base_dir.rstrip("/")) + 1

    for root, _, files in os.walk(base_dir):
        for f in files:
            if f == ".DS_Store":
                continue  # Ignore junk files
            abs_path = os.path.join(root, f)
            rel_path = abs_path[base_len:].replace("\\", "/")
            local[rel_path] = abs_path

    return local


def get_capture_date_from_exif(path):
    """Extract capture date from EXIF metadata."""
    with open(path, 'rb') as f:
        tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        date_tag = tags.get("EXIF DateTimeOriginal")
        if date_tag:
            # Convert EXIF date format to ISO
            try:
                dt = datetime.strptime(str(date_tag), "%Y:%m:%d %H:%M:%S")
                return dt.isoformat()
            except Exception:
                return None
        return None


def normalize_capture_date(date_str):
    """Extract only the YYYY-MM-DDTHH:MM:SS part from the date string, stripping whitespace."""
    if not date_str:
        return None
    date_str = date_str.strip()
    # Always take the first 19 characters (YYYY-MM-DDTHH:MM:SS)
    return date_str[:19]


# ---------- Main ----------

def main():
    # Load JSON
    with open(JSON_PATH, "r") as f:
        data = json.load(f)

    photos = data.get("photos", {})

    # Filter photos to only include those with flag status "picked"
    photos = {asset_id: info for asset_id, info in photos.items() if info.get("flag") == "pick"}

    # Build a lookup of json assets by normalized capture date
    date_to_asset_ids = {}
    for asset_id, info in photos.items():
        capture_date = info.get("captureDate")
        norm_date = normalize_capture_date(capture_date)
        if norm_date:
            date_to_asset_ids.setdefault(norm_date, []).append(asset_id)
        else:
            print(f"[ERROR] No capture date for JSON asset ID '{asset_id}'")

    # Scan disk
    local_files = scan_local_files(BASE_DIR)

    # For each file on disk, try to match by EXIF capture date
    unmatched_files = []
    for rel_path, abs_path in local_files.items():
        exif_date = get_capture_date_from_exif(abs_path)
        norm_exif_date = normalize_capture_date(exif_date)
        # print(f"File: {rel_path}, EXIF Date: '{exif_date}', Normalized EXIF Date: '{norm_exif_date}'")
        if not norm_exif_date:
            print(f"[WARN] No EXIF capture date for file '{rel_path}'")
            unmatched_files.append(rel_path)
            continue
        asset_ids = date_to_asset_ids.get(norm_exif_date)
        if not asset_ids:
            print(f"[ERROR] No JSON entry for file '{rel_path}' with capture date '{norm_exif_date}'")
            unmatched_files.append(rel_path)
        elif len(asset_ids) > 1:
            print(f"[WARN] Ambiguous match for file '{rel_path}' with capture date '{norm_exif_date}' (multiple JSON entries)")
            unmatched_files.append(rel_path)
        else:
            asset_id = asset_ids[0]
            photos[asset_id]["finalEdit"] = rel_path

    if not unmatched_files:
        print("✔ All files on disk matched JSON entries by capture date")

    # Write updated JSON safely to a new file
    json_dir, json_file = os.path.split(JSON_PATH)
    json_name, json_ext = os.path.splitext(json_file)
    new_json_path = os.path.join(json_dir, f"{json_name}_finals{json_ext}")

    with open(new_json_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nDone. JSON updated and written to {new_json_path}.")


if __name__ == "__main__":
    main()
