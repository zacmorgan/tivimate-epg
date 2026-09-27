# Daily IPTV guide for TiviMate

This folder contains a ready-to-upload GitHub project. It downloads every country feed listed at [IPTV-EPG.org](https://iptv-epg.org/guides), merges them, validates the result, and publishes `all-guides.xml.gz` as a release download. A source report accompanies each guide.

**TiviMate EPG URL:** https://github.com/zacmorgan/tivimate-epg/releases/latest/download/all-guides.xml.gz

The URL becomes available after the first successful workflow run. Future daily builds keep the same URL.

## Set up hosting

1. Create a **public** GitHub repository, for example `personal-epg`. Public access is needed so TiviMate can download the guide without a GitHub login. Your project files and generated guide will be public.
2. Upload or commit the contents of this folder to the repository's default branch. Keep `update_epg.py` at the repository root and preserve the hidden `.github/workflows/update-guide.yml` path. Include `.gitignore`. Do not put the containing `tivimate-github` folder around these files in the repository.
3. Open the repository's **Actions** tab, enable workflows if prompted, select **Update IPTV guide**, and choose **Run workflow** on the default branch. The workflow uses GitHub's built-in token; no personal access token or added secret is needed. The repository or organization must permit Actions and the workflow's `contents: write` permission.
4. Wait for the run to finish successfully. Its summary prints your actual EPG URL. You can also find `all-guides.xml.gz` and `all-guides-report.json` under the latest release.

The stable download URL has this form; replace `OWNER` and `REPOSITORY` with your actual names:

```text
https://github.com/zacmorgan/tivimate-epg/releases/latest/download/all-guides.xml.gz
```

Use this GitHub URL, rather than the temporary destination URL shown after a download redirect. Keep the asset filename the same on later runs.

Do not commit the large guide file to the repository. The workflow uploads it separately as a release asset. GitHub's [release asset limit is under 2 GiB per file](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases), which accommodates the current combined guide. [Standard GitHub-hosted runners are free for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Use the guide in TiviMate

Add the stable URL as an EPG source, assign that source to your IPTV playlist, and refresh EPG data. The file is compressed XMLTV (`.xml.gz`). Playlist channel identifiers (`tvg-id`) need to match the guide's channel IDs; unmatched channels may need manual EPG assignment in the player. This project preserves the source IDs, so it does not automatically remap a different playlist's IDs.

The combined guide covers every listed country and is large. Its download and import can take time and device storage. If needed, the updater supports `--countries ca,us,gb` to build a smaller selection; adding that option to the workflow changes future releases to those countries only.

## Updates and failure behavior

- The workflow is scheduled daily at **16:23 UTC**. You can also run it manually from Actions.
- Scheduled workflows run only from the default branch. GitHub can delay or drop scheduled runs during heavy load, and public-repository schedules are automatically disabled after **60 days without repository activity**. Check Actions periodically and re-enable a disabled workflow when necessary. See [GitHub's scheduling documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
- Every run requests fresh source files. A failed source download, invalid XML, or failed validation stops publication; the previous latest release remains available.
- A successful build is uploaded to a uniquely named draft release before that release is published and marked latest. Existing releases and assets are never deleted or overwritten. An interrupted upload may leave an unpublished draft, which does not replace the previous working guide.
- The URL remains the same as new releases become latest. GitHub documents this [latest-asset URL format](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases).
- The guide contains whatever schedule data the upstream feeds currently provide. A successful update cannot fill upstream gaps or fix an incorrect upstream schedule.

## Project contents

- `update_epg.py`: Python standard-library downloader, merger, and validator.
- `.github/workflows/update-guide.yml`: daily/manual build and release publication.
- `.gitignore`: excludes generated guides, source downloads, and temporary files.

The workflow uses Python 3.13 and official actions pinned to the verified release commits for [checkout v7.0.1](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1) and [setup-python v7.0.0](https://github.com/actions/setup-python/commit/5fda3b95a4ea91299a34e894583c3862153e4b97).

For an optional local run with Python 3.10 or later:

```sh
python3 update_epg.py --output dist/all-guides.xml.gz --work-dir work/epg-cache --cache-hours 0
```
