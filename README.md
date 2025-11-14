# Lightroom Backup

## What is it?

Unlike Lightroom Classic, Lightroom CC stores all of your photos and metadata in Adobe’s cloud. Lightroom CC offers an option to keep a local copy of your original photos (see below), but this only includes the image files - not your organisational data.

If you ever lost access to your Adobe account or Lightroom cloud storage, you would lose album structures, flags, and other information.

This script provides a programmatic way to back up your Lightroom organisation metadata, including:

- Album and folder membership
- Photo flags (picked, rejected, unflagged)

The goal is to make it possible to reconstruct your Lightroom organisation if needed, using only your local photo copies and this metadata backup.

<!-- markdownlint-disable MD033 -->
<img src="resources/screenshot.png" width="600" alt="Lightroom Cache Preferences" />

## Limitations

- Performs full backups only — incremental backups are not yet supported
- Backs up album membership and flag status only (not full Lightroom metadata)
- No restore functionality at this stage

## TODO

- Publish a Docker image for easy deployment
- Add support for incremental backups
- Improve performance for large libraries
- Backup additional metadata
- (Maybe) Explore “Lightroom-Backup-as-a-Service” hosting option
- (Maybe) Build a restore tool targeting an open-source DAM as a local Lightroom mirror

## Testing

This script has not been thoroughly tested. It has been run successfully on my Lightroom library of ~175k photos (execution time ≈ 2 hours).

All API interactions are read-only (GET requests only), so it _should_ not modify or corrupt your Lightroom data.

That said, it’s provided as-is with no warranty. **Use at your own risk**.

## Known Issues

- The script needs an API client ID and secret. Sign up in the [Adobe Developer Console](https://developer.adobe.com/console) then create a project and add Oauth Web App credentials.
- The cached authentication token is only valid for 24 hours. If a backup starts near the end of that window, the token may expire mid-run and the script will fail.
