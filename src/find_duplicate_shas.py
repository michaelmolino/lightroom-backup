import json
import argparse
from collections import defaultdict

def main():
    parser = argparse.ArgumentParser(description="Find duplicate SHA256s in Lightroom backup JSON.")
    parser.add_argument("json_path", help="Path to the Lightroom backup JSON file")
    args = parser.parse_args()

    sha_to_assets = defaultdict(list)

    with open(args.json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        photos = data.get("photos", {})

        for info in photos.values():
            sha = info.get("sha256")
            if sha:
                sha_to_assets[sha].append(info)

    duplicates = {sha: assets for sha, assets in sha_to_assets.items() if len(assets) > 1}

    if duplicates:
        print("Duplicate SHA256 values found:")
        for sha, assets in duplicates.items():
            print(f"\nSHA256: {sha}")
            for info in assets:
                file_name = info.get("fileName", "<no filename>")
                capture_date = info.get("captureDate", "<no date>")
                albums = info.get("albumPaths", [])
                print(f"  File: {file_name}")
                print(f"    Capture Date: {capture_date}")
                print(f"    Albums: {', '.join(albums) if albums else '<none>'}")
    else:
        print("No duplicate SHA256 values found.")

if __name__ == "__main__":
    main()