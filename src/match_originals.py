import os
import json
import hashlib
from datetime import datetime

# --- CONFIG ---
BASE_DIR = "/Volumes/Home Media/Cloud Backup/Lightroom CC/29937f4f4c00466aac6b6dc39e4cfd73/originals"
BACKUPS_DIR = "./backups"

# Ensure BACKUPS_DIR is an absolute path
BACKUPS_DIR = os.path.abspath(BACKUPS_DIR)

# Find the latest backup file dynamically
backup_files = [f for f in os.listdir(BACKUPS_DIR) if f.startswith("lightroom_backup_") and f.endswith(".json")]
if not backup_files:
    raise FileNotFoundError("No backup files found in the backups directory.", BACKUPS_DIR)

latest_backup = max(backup_files, key=lambda f: f)
JSON_PATH = os.path.join(BACKUPS_DIR, latest_backup)
# -------------


# ---------- Utilities ----------

def normalize(rel):
    """Normalize relative file paths for consistent comparison."""
    return rel.strip("/").replace("\\", "/").lower()


def sha256_of_file(path):
    """Compute SHA256 only when needed."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_expected_path(base_dir, capture_date, filename):
    """Build expected YYYY/YYYY-MM-DD/filename path from Lightroom metadata."""
    try:
        dt = datetime.fromisoformat(capture_date.replace("Z", ""))
    except Exception:
        return None
    year = str(dt.year)
    date_str = dt.strftime("%Y-%m-%d")
    return os.path.join(base_dir, year, date_str, filename)


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


# ---------- Main ----------

def main():
    # Load JSON
    with open(JSON_PATH, "r") as f:
        data = json.load(f)

    photos = data.get("photos", {})

    # Build path → asset maps
    expected_paths = {}   # rel_path → asset_id
    expected_sha = {}     # rel_path → sha256

    for asset_id, info in photos.items():
        filename = info.get("fileName")
        capture_date = info.get("captureDate")
        sha = info.get("sha256")

        if not filename or not capture_date or not sha:
            print(f"[WARN] Asset {asset_id} missing required metadata")
            continue

        abs_expected = build_expected_path(BASE_DIR, capture_date, filename)
        if not abs_expected:
            print(f"[WARN] Asset {asset_id} invalid captureDate: {capture_date}")
            continue

        rel_path = abs_expected[len(BASE_DIR.rstrip('/')) + 1:].replace("\\", "/")
        expected_paths[rel_path] = asset_id
        expected_sha[rel_path] = sha

    # Scan disk
    local_files = scan_local_files(BASE_DIR)

    # Step 1 — primary matches
    unmatched_json = []
    for rel_path, asset_id in expected_paths.items():
        abs_path = local_files.get(rel_path)
        if abs_path and os.path.exists(abs_path):
            photos[asset_id]["localPath"] = rel_path
        else:
            unmatched_json.append((asset_id, rel_path, expected_sha[rel_path]))

    # Build set of valid matched local files (normalised)
    matched_local_set = {
        normalize(info["localPath"])
        for info in photos.values()
        if "localPath" in info and os.path.exists(os.path.join(BASE_DIR, info["localPath"]))
    }

    # Step 2 — SHA fallback for JSON assets not matched above
    if unmatched_json:
        candidates = {
            rel: abs_path for rel, abs_path in local_files.items()
            if normalize(rel) not in matched_local_set
        }

        sha_to_local = {}

        # Only hash the candidate files (tiny subset)
        for rel, abs_path in candidates.items():
            sha_to_local.setdefault(sha256_of_file(abs_path), rel)

        still_unmatched = []
        for asset_id, rel_path, sha in unmatched_json:
            if sha in sha_to_local:
                # SHA match found → link it
                matched_rel = sha_to_local[sha]
                photos[asset_id]["localPath"] = matched_rel
                matched_local_set.add(normalize(matched_rel))
            else:
                still_unmatched.append((asset_id, rel_path, sha))

        unmatched_json = still_unmatched

    # Step 3 — find local files not represented in JSON
    unmatched_local = []
    json_shas = {info["sha256"] for info in photos.values() if "sha256" in info}

    # Candidates not already matched
    candidate_local_files = {
        rel: abs_path for rel, abs_path in local_files.items()
        if normalize(rel) not in matched_local_set
    }

    # Hash these candidates once each
    sha_to_local = {}
    for rel, abs_path in candidate_local_files.items():
        sha_to_local.setdefault(sha256_of_file(abs_path), rel)

    # If a SHA exists in JSON but no localPath is assigned, link it
    for sha, rel in sha_to_local.items():
        if sha in json_shas:
            for asset_id, info in photos.items():
                if info.get("sha256") == sha:
                    if "localPath" not in info or not info["localPath"]:
                        photos[asset_id]["localPath"] = rel
                        matched_local_set.add(normalize(rel))
                    break
        else:
            unmatched_local.append((rel, sha))

    # Print unmatched results
    if unmatched_json:
        print("\n=== JSON assets missing on disk ===")
        for asset_id, rel, sha in unmatched_json:
            print(f"❌ JSON asset {asset_id} expects '{rel}', SHA={sha}")
    else:
        print("✔ All JSON assets matched")

    if unmatched_local:
        print("\n=== Local files not found in JSON ===")
        for rel, sha in unmatched_local:
            print(f"❌ Local file '{rel}', SHA={sha}")
    else:
        print("✔ All local files matched JSON")

    # Write updated JSON safely to a new file
    json_dir, json_file = os.path.split(JSON_PATH)
    json_name, json_ext = os.path.splitext(json_file)
    new_json_path = os.path.join(json_dir, f"{json_name}_local{json_ext}")

    with open(new_json_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nDone. JSON updated and written to {new_json_path}.")


if __name__ == "__main__":
    main()
