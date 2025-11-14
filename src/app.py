import json
import requests
import ssl
import os
from urllib.parse import urlencode
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

CLIENT_ID = os.environ.get("LIGHTROOM_CLIENT_ID") or None
CLIENT_SECRET = os.environ.get("LIGHTROOM_CLIENT_SECRET") or None

OUTPUT_FILE = f"./backups/lightroom_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
AUTH_CACHE_FILE = ".lightroom-backup-authentication.json"

REDIRECT_URI = "https://localhost:8080/callback"
AUTH_URL = "https://ims-na1.adobelogin.com/ims/authorize"
TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token"
API_BASE = "https://lr.adobe.io/v2"

PAGE_SIZE = 200

def load_cached_token():
    """Load cached OAuth token JSON from AUTH_CACHE_FILE, or return None."""
    try:
        if os.path.exists(AUTH_CACHE_FILE):
            with open(AUTH_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        # Ignore cache read errors; treat as no cache
        pass
    return None

def save_token(token_data: dict):
    """Persist token JSON to AUTH_CACHE_FILE."""
    try:
        with open(AUTH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(token_data, f, indent=2)
    except Exception as e:
        print(f"Warning: failed to write auth cache: {e}")

def get_authorization_code():
    params = {
        "client_id": CLIENT_ID,
        "scope": "openid lr_partner_apis",
        "response_type": "code",
        "redirect_uri": REDIRECT_URI
    }
    url = f"{AUTH_URL}?{urlencode(params)}"

    # Container to store the auth code
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

    # Create SSL context with self-signed certificate
    cert_file = "./certs/cert.pem"
    key_file = "./certs/key.pem"
    
    # Generate self-signed certificate if it doesn't exist
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        print("Generating self-signed SSL certificate...")
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
        print("Certificate generated.\n")
    
    print(f"Open this URL in your browser and log in:\n\n{url}\n")
 
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    
    # Wrap socket with SSL
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    
    # Handle multiple requests in case browser makes multiple attempts
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
    
    # Adobe Lightroom API returns JSON with a security prefix
    text = r.text
    if text.startswith("while (1) {}"):
        text = text[12:]  # Remove the "while (1) {}" prefix
    
    return json.loads(text)

def get_album_info(album):
    """Extract album information including name and parent path."""
    album_id = album["id"]
    payload = album.get("payload", {})
    
    # Get album name
    name = (
        payload.get("name") or 
        payload.get("source") or 
        album.get("subtype") or 
        f"album_{album_id}"
    )
    
    # Get parent album ID for hierarchy
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
        print(f"    Fetched page {page}: {len(resources)} assets (total: {len(assets)})", flush=True)
        page += 1
        
        # Get the next URL correctly - it's a dict with "href" key
        next_link = data.get("links", {}).get("next", {})
        if isinstance(next_link, dict):
            href = next_link.get("href")
            if href:
                # The href is relative and needs catalog ID prepended
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
            print(f"  Warning: could not fetch flag '{flag_value}': {e}")
    return flags_map

def get_stack_child_assets(catalog_id, embedded_asset: dict, headers):
    """Fetch child assets for a stack using the rels/stack_assets link when present."""
    links = (embedded_asset or {}).get("links", {})
    rel = links.get("/rels/stack_assets", {})
    href = rel.get("href") if isinstance(rel, dict) else None
    if not href:
        return []

    # Build absolute URL similar to pagination logic
    if href.startswith("http"):
        url = href
    elif href.startswith("catalogs/"):
        url = f"{API_BASE}/{href}"
    else:
        url = f"{API_BASE}/catalogs/{catalog_id}/{href}"

    # Ensure we embed asset payloads to extract importSource
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
        print(f"    Skipping asset {underlying_id} with no filename or sha256.")
        return  # Nothing meaningful to record
    resolved_flag = flags_map.get(underlying_id)
    metadata["flag"] = resolved_flag
    
    # Track in album
    if underlying_id not in backup_data["albums"][album_path]["asset_ids"]:
        backup_data["albums"][album_path]["asset_ids"].append(underlying_id)
    
    # Merge album paths if asset already exists
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
    print(f"  Found {len(flags_map)} assets...")

    print("  Fetching assets...")
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
    
    print(f"\nProcessing {len(albums)} albums...")
    for idx, album in enumerate(albums, 1):
        album_id = album["id"]
        album_info = album_map[album_id]
        album_path = get_album_full_path(album_id, album_map)
        
        print(f"[{idx}/{len(albums)}] Album: {album_path}")
        
        backup_data["albums"][album_path] = {
            "id": album_id,
            "subtype": album_info["subtype"],
            "created": album_info["created"],
            "updated": album_info["updated"],
            "parent_id": album_info["parent_id"],
            "asset_ids": []
        }
        
        asset_count = process_album_assets(catalog_id, album_id, album_path, headers, backup_data)
        print(f"  ✓ Completed album: {album_path} ({asset_count} assets)\n")
    
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
            print("Using cached auth token.")
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                print("Cached token invalid or expired. Starting OAuth login...")
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
    print("\nFetching albums...")
    albums = []
    albums_url = f"{API_BASE}/catalogs/{catalog_id}/albums?limit={PAGE_SIZE}"
    page = 1
    
    while albums_url:
        data = get_json(albums_url, headers)
        resources = data.get("resources", [])
        albums.extend(resources)
        print(f"  Fetched page {page}: {len(resources)} albums (total: {len(albums)})", flush=True)
        page += 1
        albums_url = get_next_page_url(data, catalog_id)
    
    print(f"Found {len(albums)} total albums.")
    return albums

def main():
    print("Starting Adobe Lightroom metadata backup...")
    
    headers, catalog_data = get_authenticated_headers_and_catalog()
    catalog_id = catalog_data["id"]
    print(f"Catalog ID: {catalog_id}")

    albums = fetch_all_albums(catalog_id, headers)
    backup_data = process_albums(catalog_id, albums, headers)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2)

    print(f"\nBackup complete. Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
