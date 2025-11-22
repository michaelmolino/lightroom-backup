import os
import json
import requests
import ssl
import hashlib
import exifread
from datetime import datetime
from urllib.parse import urlencode
from http.server import BaseHTTPRequestHandler, HTTPServer
import argparse
import logging
import re

# Config 
API_BASE = "https://lr.adobe.io/v2"
REDIRECT_URI = "https://localhost:8080/callback"
AUTH_URL = "https://ims-na1.adobelogin.com/ims/authorize"
TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token"
CLIENT_ID = os.environ.get("LIGHTROOM_CLIENT_ID") or None
CLIENT_SECRET = os.environ.get("LIGHTROOM_CLIENT_SECRET") or None
AUTH_CACHE_FILE = ".lightroom-backup-authentication.json"
PAGE_SIZE = 200

OUTPUT_FILE = f"./backups/lightroom_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
ORIGINALS_DIR = os.environ.get("LIGHTROOM_ORIGINALS_DIR") or "/data/originals"
EXPORTS_DIR = os.environ.get("LIGHTROOM_EXPORTS_DIR") or "/data/exports"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler("app.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("lightroom-app")

# Helpers
def normalize(rel):
    return rel.strip("/").replace("\\", "/").lower()

def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def scan_local_files(base_dir):
    rel_paths = set()
    base_len = len(base_dir.rstrip("/")) + 1
    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.startswith("."):
                continue
            abs_path = os.path.join(root, f)
            rel_path = abs_path[base_len:].replace("\\", "/")
            rel_paths.add(rel_path)
    return rel_paths

def get_capture_date_from_exif(path):
    with open(path, 'rb') as f:
        tags = exifread.process_file(f, details=False)
        candidates = []
        # Check all tags ending with 'DateTimeOriginal' or 'Date/Time Original' (case-insensitive)
        for key, date_tag in tags.items():
            key_lower = key.lower()
            if key_lower.endswith('datetimeoriginal') or key_lower.endswith('date/time original'):
                date_str = str(date_tag)
                # Match with optional fractional seconds
                match = re.match(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", date_str)
                if match:
                    year, month, day, hour, minute, second, frac = match.groups()
                    iso = f"{year}-{month}-{day}T{hour}:{minute}:{second}"
                    if frac:
                        iso += f".{frac}"
                    candidates.append(iso)
        # Prefer the one with the most precision (longest string)
        if candidates:
            candidates.sort(key=len, reverse=True)
            return candidates[0]
        return None

def normalize_capture_date(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    # Remove trailing Z or timezone offset, keep fractional seconds
    # Normalize: drop fractional seconds if all zeros (e.g., .00, .000)
    match = re.match(r"^([\dT:\-]+:\d{2})(\.\d+)?(?:Z|[+-]\d+:?\d*)?$", date_str)
    if match:
        base = match.group(1)
        frac = match.group(2)
        if frac and re.fullmatch(r"\.0+", frac):
            return base
        elif frac:
            return base + frac
        else:
            return base
    match2 = re.match(r"^([0-9T:\.-]+)", date_str)
    if match2:
        return match2.group(1)
    return date_str

def load_cached_token():
    """Load cached OAuth token JSON from AUTH_CACHE_FILE, or return None."""
    try:
        if os.path.exists(AUTH_CACHE_FILE):
            with open(AUTH_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def save_token(token_data: dict):
    """Persist token JSON to AUTH_CACHE_FILE."""
    try:
        with open(AUTH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(token_data, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to write auth cache: {e}")

def get_authorization_code():
    params = {
        "client_id": CLIENT_ID,
        "scope": "openid lr_partner_apis",
        "response_type": "code",
        "redirect_uri": REDIRECT_URI
    }
    url = f"{AUTH_URL}?{urlencode(params)}"

    auth_code_container: dict[str, str | None] = {"code": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            code = query.get("code", [None])[0]
            if code:
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Success! You may close this window.</h2></body></html>")
                auth_code_container["code"] = code
            else:
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Error: No code found.</h2></body></html>")

    cert_file = "./certs/cert.pem"
    key_file = "./certs/key.pem"
    
    # Generate self-signed certificate if it doesn't exist
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        log.info("Generating self-signed SSL certificate...")
        import subprocess
        san = "DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1"
        try:
            # Try modern OpenSSL with -addext
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:4096",
                "-keyout", key_file, "-out", cert_file,
                "-days", "365", "-nodes",
                "-subj", "/CN=localhost",
                "-addext", f"subjectAltName={san}"
            ], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback: generate without SAN (browser will warn, but it works)
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:4096",
                "-keyout", key_file, "-out", cert_file,
                "-days", "365", "-nodes",
                "-subj", "/CN=localhost"
            ], check=True, capture_output=True)
        log.info("Certificate generated.")
    
    print(f"Open this URL in your browser and log in:\n\n{url}\n")
 
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    
    while auth_code_container["code"] is None:
        server.handle_request()
    
    return auth_code_container["code"]

def get_access_token(auth_code):
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI
    }
    r = requests.post(TOKEN_URL, data=data)
    r.raise_for_status()
    return r.json()

def get_json(url, headers):
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    
    text = r.text
    if text.startswith("while (1) {}"):
        text = text[12:]
    
    return json.loads(text)

def get_album_info(album):
    """Extract album information including name and parent path."""
    album_id = album["id"]
    payload = album.get("payload", {})
    
    name = (
        payload.get("name") or 
        payload.get("source") or 
        album.get("subtype") or 
        f"album_{album_id}"
    )
    
    parent_id = payload.get("parent", {}).get("id") if "parent" in payload else None
    
    return {
        "id": album_id,
        "name": name,
        "parent_id": parent_id,
        "subtype": album.get("subtype"),
        "created": album.get("created"),
        "updated": album.get("updated")
    }

def fetch_all_assets(catalog_id, album_id, headers):
    """Fetch all assets for a given album with embedded asset data (asset only)."""
    album_assets_url = f"{API_BASE}/catalogs/{catalog_id}/albums/{album_id}/assets?limit={PAGE_SIZE}&embed=asset"
    assets = []
    page = 1
    
    while album_assets_url:
        data = get_json(album_assets_url, headers)
        resources = data.get("resources", [])
        assets.extend(resources)
        log.info(f"Fetched page {page}: {len(resources)} assets (total: {len(assets)})")
        page += 1
        
        next_link = data.get("links", {}).get("next", {})
        if isinstance(next_link, dict):
            href = next_link.get("href")
            if href:
                if not href.startswith("http"):
                    album_assets_url = f"{API_BASE}/catalogs/{catalog_id}/{href}"
                else:
                    album_assets_url = href
            else:
                album_assets_url = None
        else:
            album_assets_url = None
    
    return assets

def get_next_page_url(data, catalog_id):
    """Extract the next page URL from API response."""
    next_link = data.get("links", {}).get("next", {})
    if not isinstance(next_link, dict):
        return None
    
    href = next_link.get("href")
    if not href:
        return None
    
    if href.startswith("http"):
        return href
    
    return f"{API_BASE}/catalogs/{catalog_id}/{href}"

def fetch_asset_ids_by_flag(catalog_id, album_id, headers, flag_value):
    """Fetch only asset IDs for a given album filtered by flag (pick|unflagged|reject)."""
    url = f"{API_BASE}/catalogs/{catalog_id}/albums/{album_id}/assets?limit={PAGE_SIZE}&flag={flag_value}&embed=asset"
    ids = set()
    while url:
        data = get_json(url, headers)
        resources = data.get("resources", [])
        for r in resources:
            # Prefer underlying asset id if embedded, else fall back to relation id
            rid = (r.get("asset") or {}).get("id") or r.get("id")
            if rid:
                ids.add(rid)
        url = get_next_page_url(data, catalog_id)
    return ids

def build_flags_map(catalog_id, album_id, headers):
    """Build mapping of asset_id -> flag by querying album assets with flag filters."""
    flags_map = {}
    for flag_value in ("pick", "unflagged", "reject"):
        try:
            ids = fetch_asset_ids_by_flag(catalog_id, album_id, headers, flag_value)
            for rid in ids:
                flags_map[rid] = flag_value
        except Exception as e:
            log.error(f"Could not fetch flag '{flag_value}': {e}")
    return flags_map

def get_stack_child_assets(catalog_id, embedded_asset: dict, headers):
    """Fetch child assets for a stack using the rels/stack_assets link when present."""
    links = (embedded_asset or {}).get("links", {})
    rel = links.get("/rels/stack_assets", {})
    href = rel.get("href") if isinstance(rel, dict) else None
    if not href:
        return []

    if href.startswith("http"):
        url = href
    elif href.startswith("catalogs/"):
        url = f"{API_BASE}/{href}"
    else:
        url = f"{API_BASE}/catalogs/{catalog_id}/{href}"

    if "embed=" not in url:
        sep = '&' if '?' in url else '?'
        url = f"{url}{sep}embed=asset"

    try:
        data = get_json(url, headers)
        return data.get("resources", [])
    except Exception:
        return []

def extract_asset_metadata(asset_resource):
    """Extract metadata from embedded asset data.

    Prefer payload.importSource for fileName and sha256 as per API guidance,
    with a fallback to payload.origin if importSource is missing.
    """
    asset_data = asset_resource.get("asset", {}) if isinstance(asset_resource, dict) else {}
    payload = asset_data.get("payload", {}) if isinstance(asset_data, dict) else {}

    import_source = payload.get("importSource", {}) if isinstance(payload.get("importSource"), dict) else {}
    origin = payload.get("origin", {}) if isinstance(payload.get("origin"), dict) else {}

    file_name = import_source.get("fileName") or origin.get("fileName")
    sha256 = import_source.get("sha256") or origin.get("sha256")

    return {
        "fileName": file_name,
        "sha256": sha256,
        "captureDate": payload.get("captureDate")
    }

def build_album_hierarchy(albums):
    """Build album hierarchy map with ID lookup."""
    album_map = {}
    for album in albums:
        info = get_album_info(album)
        album_map[info["id"]] = info
    return album_map

def get_album_full_path(album_id, album_map):
    """Build full path for an album considering parent relationships."""
    if album_id not in album_map:
        return None
    album = album_map[album_id]
    if album["parent_id"] and album["parent_id"] in album_map:
        parent_path = get_album_full_path(album["parent_id"], album_map)
        return f"{parent_path}/{album['name']}" if parent_path else album["name"]
    return album["name"]

def get_resources_to_record(catalog_id, asset, headers):
    """Get the list of resources to record for an asset (stack children or the asset itself)."""
    embedded = asset.get("asset", {}) if isinstance(asset, dict) else {}
    subtype = embedded.get("subtype")
    
    if subtype == "stack":
        children = get_stack_child_assets(catalog_id, embedded, headers)
        return children if children else [asset]
    return [asset]

def record_asset_in_backup(res, album_path, flags_map, backup_data):
    """Record a single asset resource in the backup data."""
    underlying_id = (res.get("asset") or {}).get("id") or res.get("id")
    if not underlying_id:
        return
    
    metadata = extract_asset_metadata(res)
    # Skip placeholder/empty stack parent or unusable asset entries that have no identifying metadata
    if (
        metadata.get("fileName") is None
        and metadata.get("sha256") is None
    ):
        log.info(f"Skipping asset {underlying_id} with no filename or sha256.")
        return
    resolved_flag = flags_map.get(underlying_id)
    metadata["flag"] = resolved_flag
    
    if underlying_id not in backup_data["albums"][album_path]["asset_ids"]:
        backup_data["albums"][album_path]["asset_ids"].append(underlying_id)
    
    existing_albums = backup_data["photos"].get(underlying_id, {}).get("albumPaths", [])
    if album_path not in existing_albums:
        existing_albums = existing_albums + [album_path]
    
    backup_data["photos"][underlying_id] = {
        **metadata,
        "albumPaths": existing_albums
    }

def process_album_assets(catalog_id, album_id, album_path, headers, backup_data):
    """Process all assets for a single album."""
    flags_map = build_flags_map(catalog_id, album_id, headers)
    log.info(f"Found {len(flags_map)} assets...")

    log.info("Fetching assets...")
    assets = fetch_all_assets(catalog_id, album_id, headers)
    
    for asset in assets:
        resources_to_record = get_resources_to_record(catalog_id, asset, headers)
        for res in resources_to_record:
            record_asset_in_backup(res, album_path, flags_map, backup_data)
    
    return len(assets)

def process_albums(catalog_id, albums, headers):
    """Process all albums and their assets."""
    backup_data = {"albums": {}, "photos": {}}
    album_map = build_album_hierarchy(albums)
    
    log.info(f"Processing {len(albums)} albums...")
    for idx, album in enumerate(albums, 1):
        album_id = album["id"]
        album_info = album_map[album_id]
        album_path = get_album_full_path(album_id, album_map)
        
        log.info(f"[{idx}/{len(albums)}] Album: {album_path}")
        
        backup_data["albums"][album_path] = {
            "id": album_id,
            "subtype": album_info["subtype"],
            "created": album_info["created"],
            "updated": album_info["updated"],
            "parent_id": album_info["parent_id"],
            "asset_ids": []
        }
        
        asset_count = process_album_assets(catalog_id, album_id, album_path, headers, backup_data)
        log.info(f"✓ Completed album: {album_path} ({asset_count} assets)")
    
    return backup_data

def get_authenticated_headers_and_catalog():
    """Get authenticated headers and catalog data, using auth cache or OAuth flow."""
    token_data = load_cached_token()
    headers = None
    catalog_data = None

    # Try cached token first
    if token_data and token_data.get("access_token"):
        headers = {"Authorization": f"Bearer {token_data['access_token']}", "x-api-key": CLIENT_ID}
        try:
            catalog_data = get_json(f"{API_BASE}/catalog", headers)
            log.info("Using cached auth token.")
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                log.info("Cached token invalid or expired. Starting OAuth login...")
                headers = None
                catalog_data = None
            else:
                raise

    # If no valid cached token, perform OAuth flow
    if catalog_data is None:
        auth_code = get_authorization_code()
        token_data = get_access_token(auth_code)
        access_token = token_data["access_token"]
        headers = {"Authorization": f"Bearer {access_token}", "x-api-key": CLIENT_ID}
        save_token(token_data)
        catalog_data = get_json(f"{API_BASE}/catalog", headers)

    return headers, catalog_data

def fetch_all_albums(catalog_id, headers):
    """Fetch all albums with pagination."""
    log.info("Fetching albums...")
    albums = []
    albums_url = f"{API_BASE}/catalogs/{catalog_id}/albums?limit={PAGE_SIZE}"
    page = 1
    
    while albums_url:
        data = get_json(albums_url, headers)
        resources = data.get("resources", [])
        albums.extend(resources)
        log.info(f"Fetched page {page}: {len(resources)} albums (total: {len(albums)})")
        page += 1
        albums_url = get_next_page_url(data, catalog_id)
    
    log.info(f"Found {len(albums)} total albums.")
    return albums

def match_files_to_assets(photos, originals_dir, exports_dir):
    log.info("Scanning originals directory...")
    originals = scan_local_files(originals_dir)
    log.info(f"Found {len(originals)} original files.")

    log.info("Scanning exports/edits directory...")
    exports = scan_local_files(exports_dir)
    log.info(f"Found {len(exports)} export/edit files.")

    # matched_rel_paths is the set of original files matched by filename/path
    # this is the preferred way to match originals to assets
    matched_rel_paths = _match_originals_by_filename_path(photos, originals)
    unmatched_files = originals - matched_rel_paths
    log.info(f"Unmatched files which will fall back to SHA256 matching: {len(unmatched_files)}")

    # Now try to match unmatched originals by SHA256
    # Only has local files that were not matched by filename/path
    _match_originals_by_sha256(photos, unmatched_files, originals_dir)
    _log_missing_local_originals(photos)

    # Now match exports/edits by EXIF capture date
    # Since these files are edited and renamed, we can't fall back to filename/path or SHA256
    # We can only rely on exact time date matches
    # date_to_asset_ids matches the capture date to an array of unique asset IDs
    date_to_asset_ids = {}
    for asset_id, info in photos.items():
        norm_date = normalize_capture_date(info.get("captureDate"))
        if norm_date:
            date_to_asset_ids.setdefault(norm_date, []).append(asset_id)
    _match_exports_by_exif_date(photos, exports, date_to_asset_ids, exports_dir)

def _match_originals_by_filename_path(photos, originals):
    matched_rel_paths = set()
    for asset_id, info in photos.items():
        file_name = info.get("fileName")
        capture_date = info.get("captureDate")
        if not file_name or not capture_date:
            continue
        try:
            dt = capture_date.split("T")[0]  # 'YYYY-MM-DD'
            yyyy = dt[:4]
            rel_path = f"{yyyy}/{dt}/{file_name}"
        except Exception:
            continue
        if rel_path in originals:
            photos[asset_id]["localOriginal"] = rel_path
            matched_rel_paths.add(rel_path)
    return matched_rel_paths

def _log_missing_local_originals(photos):
    missing = 0
    for asset_id, info in photos.items():
        if "localOriginal" not in info:
            log.warning(f"Asset '{asset_id}' (fileName='{info.get('fileName')}') not matched to any local original file.")
            missing += 1
    if missing:
        log.info(f"Total assets missing local original file: {missing}")

def _match_originals_by_sha256(photos, unmatched_files, originals_dir):
    # Do a linear scan once through photos to build sha256 -> asset_id map for unmatched assets
    sha_to_asset = {info.get("sha256"): asset_id for asset_id, info in photos.items() if info.get("sha256") and "localOriginal" not in info}
    log.info(f"Unmatched SHAs: {len(sha_to_asset.keys())}")  
    for rel_path in unmatched_files:
        abs_path = os.path.join(originals_dir, rel_path)
        if not os.path.exists(abs_path):
            log.warning(f"Original file '{rel_path}' expected at '{abs_path}' does not exist on disk. Skipping SHA check.")
            continue
        sha = sha256_of_file(abs_path)
        asset_id = sha_to_asset.get(sha)
        if asset_id:
            photos[asset_id]["localOriginal"] = rel_path

def _find_pick_assets_by_exact_date(photos, date_to_asset_ids, norm_exif_date):
    asset_ids = date_to_asset_ids.get(norm_exif_date)
    if not asset_ids:
        return []
    return [asset_id for asset_id in asset_ids if photos.get(asset_id, {}).get("flag") == "pick"]

def _find_pick_assets_by_prefix(photos, norm_exif_date):
    pick_assets = []
    if not norm_exif_date:
        return pick_assets
    for asset_id, info in photos.items():
        if info.get("flag") == "pick":
            cap_date = normalize_capture_date(info.get("captureDate"))
            if isinstance(cap_date, str) and cap_date.startswith(norm_exif_date):
                pick_assets.append(asset_id)
    return pick_assets

def _handle_export_match(photos, rel_path, norm_exif_date, pick_assets):
    if not pick_assets:
        log.warning(f"Export file '{rel_path}' not matched in JSON (EXIF date '{norm_exif_date}')")
    elif len(pick_assets) > 1:
        log.warning(f"Ambiguous match for export file '{rel_path}' with EXIF date '{norm_exif_date}' (multiple pick assets)")
    else:
        asset_id = pick_assets[0]
        photos[asset_id]["localEdit"] = rel_path

def _match_exports_by_exif_date(photos, exports, date_to_asset_ids, exports_dir):
    for rel_path in exports:
        abs_path = os.path.join(exports_dir, rel_path)
        exif_date = get_capture_date_from_exif(abs_path)
        norm_exif_date = normalize_capture_date(exif_date)
        pick_assets = _find_pick_assets_by_exact_date(photos, date_to_asset_ids, norm_exif_date)
        if not pick_assets:
            pick_assets = _find_pick_assets_by_prefix(photos, norm_exif_date)
        _handle_export_match(photos, rel_path, norm_exif_date, pick_assets)

# Main
def main():
    parser = argparse.ArgumentParser(description="Lightroom backup and local file matcher")
    parser.add_argument("--use-backup-json", metavar="PATH", help="Path to backup JSON file to use instead of fetching from API")
    args = parser.parse_args()

    # First we use the Lightroom API as the source of truth for assets and metadata
    # Since this is slow, we can use a local file from a previous run if we want
    if args.use_backup_json:
        log.info(f"Loading backup data from {args.use_backup_json}...")
        with open(args.use_backup_json, "r", encoding="utf-8") as f:
            backup_data = json.load(f)
    else:
        log.info("Starting Adobe Lightroom metadata backup and local file matching...")
        headers, catalog_data = get_authenticated_headers_and_catalog()
        catalog_id = catalog_data["id"]
        log.info(f"Catalog ID: {catalog_id}")
        # Fetch all albums from the API
        albums = fetch_all_albums(catalog_id, headers)
        # Process all albums including fetching their assets from the API
        backup_data = process_albums(catalog_id, albums, headers)
        # Write API-fetched backup data to disk with '_api' suffix
        api_file = OUTPUT_FILE.replace('.json', '_api.json') if OUTPUT_FILE.endswith('.json') else OUTPUT_FILE + '_api'
        with open(api_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2)
        log.info(f"API backup data saved to {api_file}")
        

    # This is the main matching logic
    match_files_to_assets(backup_data["photos"], ORIGINALS_DIR, EXPORTS_DIR)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2)
    log.info(f"Backup complete. Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
