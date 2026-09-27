#!/usr/bin/env python3
"""Download every country guide at IPTV-EPG.org and merge into one XMLTV gzip.

Python 3.10+, standard library only. Run: python3 update_epg.py
Existing output is replaced only after all sources and the merged file validate.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import gzip
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

INDEX = "https://iptv-epg.org/guides"
BASE = Path(__file__).resolve().parent
CHUNK = 1024 * 1024


def log(message):
    print(message, flush=True)


class FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key not in ("href", "data-gz", "data-xml") or not value:
                continue
            url = urllib.parse.urljoin(INDEX, value.strip())
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme == "https" and parsed.netloc == "iptv-epg.org"
                    and re.fullmatch(r"/files/epg-[a-z0-9-]+\.xml(?:\.gz)?", parsed.path)):
                path = parsed.path if parsed.path.endswith(".gz") else parsed.path + ".gz"
                self.urls.add("https://iptv-epg.org" + path)


def ssl_context():
    context = ssl.create_default_context()
    # Include macOS system roots when a standalone Python lacks its own CA bundle.
    if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").is_file():
        context.load_verify_locations("/etc/ssl/cert.pem")
    return context


def download(url, target, timeout=90):
    temporary = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Personal-XMLTV-Merger/1.0",
                "Accept-Encoding": "identity",
            })
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
                expected = response.headers.get("Content-Length")
                with temporary.open("wb") as out:
                    shutil.copyfileobj(response, out, CHUNK)
            if expected is not None and temporary.stat().st_size != int(expected):
                raise ValueError("Incomplete download")
            if target.name.endswith(".gz"):
                with temporary.open("rb") as stream:
                    if stream.read(2) != b"\x1f\x8b":
                        raise ValueError("Server did not return a gzip file")
            temporary.replace(target)
            return
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def discover(index_path):
    parser = FeedLinks()
    parser.feed(index_path.read_text(encoding="utf-8"))
    if not parser.urls:
        raise ValueError("No guide links found. The source page may have changed.")
    return sorted(parser.urls)


def elements(path):
    """Yield top-level XMLTV elements, freeing each from the tree after use."""
    with gzip.open(path, "rb") as stream:
        events = ET.iterparse(stream, events=("start", "end"))
        event, root = next(events)
        if event != "start" or root.tag != "tv":
            raise ValueError(f"{path.name}: expected an XMLTV <tv> root")
        depth = 1
        for event, element in events:
            if event == "start":
                depth += 1
            else:
                if depth == 2:
                    element.tail = None
                    yield element
                    root.remove(element)
                depth -= 1


def normalized(element):
    """Normalize indentation and attribute order, retaining actual field values."""
    for node in element.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
        attributes = sorted(node.attrib.items())
        node.attrib.clear()
        node.attrib.update(attributes)
    return ET.tostring(element, encoding="utf-8", short_empty_elements=True)


def combine_channels(existing, incoming):
    # Union display names, icons and URLs; keep the original ID for M3U matching.
    seen = {normalized(child) for child in existing}
    for child in incoming:
        encoded = normalized(child)
        if encoded not in seen:
            existing.append(child)
            seen.add(encoded)
    order = {"display-name": 0, "icon": 1, "url": 2}
    existing[:] = sorted(existing, key=lambda child: order.get(child.tag, 3))


def validate_merged(path, expected_channels, expected_programmes):
    channel_ids = set()
    programmes = 0
    for element in elements(path):
        if element.tag == "channel":
            if programmes:
                raise ValueError("A channel appears after programmes in the output")
            channel_id = element.get("id")
            if not channel_id or channel_id in channel_ids:
                raise ValueError("Missing or repeated channel ID")
            channel_ids.add(channel_id)
        elif element.tag == "programme":
            if element.get("channel") not in channel_ids:
                raise ValueError("A programme refers to an undefined channel")
            if not element.get("start") or element.find("title") is None:
                raise ValueError("A programme lacks its start time or title")
            programmes += 1
        else:
            raise ValueError(f"Unexpected XMLTV element: {element.tag}")
    if len(channel_ids) != expected_channels or programmes != expected_programmes:
        raise ValueError("Output counts do not match the merge counts")
    return {"channels": len(channel_ids), "programmes": programmes}


def merge(sources, output, working_dir):
    channels = {}
    referenced = set()
    differing_channels = set()
    source_report = []
    counts = {"channels_read": 0, "programmes_read": 0, "duplicate_programmes_removed": 0,
              "programmes_written": 0}
    with tempfile.TemporaryDirectory(prefix="merge-", dir=working_dir) as temp_name:
        temp = Path(temp_name)
        database = sqlite3.connect(temp / "seen.sqlite3")
        database.execute("PRAGMA journal_mode=OFF")
        database.execute("PRAGMA synchronous=OFF")
        database.execute("PRAGMA cache_size=-32768")
        database.execute("CREATE TABLE seen (digest BLOB PRIMARY KEY) WITHOUT ROWID")
        try:
            with gzip.open(temp / "programmes.gz", "wb", compresslevel=1) as spool:
                for index, (url, path, cached) in enumerate(sources, 1):
                    stats = {"url": url, "downloaded_at_utc": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc).isoformat(), "used_cache": cached,
                        "bytes": path.stat().st_size, "channels": 0, "programmes": 0}
                    try:
                        for element in elements(path):
                            encoded = normalized(element)
                            if element.tag == "channel":
                                channel_id = element.get("id")
                                if not channel_id or element.find("display-name") is None:
                                    raise ValueError("Channel lacks ID or display name")
                                stats["channels"] += 1
                                counts["channels_read"] += 1
                                if channel_id in channels:
                                    if normalized(channels[channel_id]) != encoded:
                                        differing_channels.add(channel_id)
                                        combine_channels(channels[channel_id], element)
                                else:
                                    channels[channel_id] = element
                            elif element.tag == "programme":
                                if not element.get("channel") or not element.get("start") or element.find("title") is None:
                                    raise ValueError("Programme lacks channel, start, or title")
                                stats["programmes"] += 1
                                counts["programmes_read"] += 1
                                referenced.add(element.get("channel"))
                                digest = hashlib.sha256(encoded).digest()
                                cursor = database.execute("INSERT OR IGNORE INTO seen VALUES (?)", (digest,))
                                if cursor.rowcount:
                                    spool.write(encoded + b"\n")
                                    counts["programmes_written"] += 1
                                else:
                                    counts["duplicate_programmes_removed"] += 1
                            else:
                                raise ValueError(f"Unexpected XMLTV element: {element.tag}")
                        if not stats["channels"] or not stats["programmes"]:
                            raise ValueError("Feed contains no channels or programmes")
                    except Exception as error:
                        # An invalid download must not be reused on the next run.
                        path.unlink(missing_ok=True)
                        raise ValueError(f"Invalid feed {url}: {error}") from error
                    database.commit()
                    source_report.append(stats)
                    log(f"Merged {index}/{len(sources)}: {path.name} ({stats['programmes']:,} programmes)")
        finally:
            database.close()
        missing = referenced - channels.keys()
        if missing:
            raise ValueError(f"Programmes reference {len(missing)} undefined channels: {sorted(missing)[:10]}")
        log(f"Writing {len(channels):,} channels and {counts['programmes_written']:,} programmes...")
        descriptor, pending_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
        import os
        os.close(descriptor)
        pending = Path(pending_name)
        try:
            with pending.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as out:
                out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                out.write(b'<tv generator-info-name="Personal XMLTV Merger" source-info-name="IPTV-EPG.org" source-info-url="https://iptv-epg.org/guides">\n')
                for channel_id in sorted(channels):
                    out.write(normalized(channels[channel_id]) + b"\n")
                with gzip.open(temp / "programmes.gz", "rb") as spool:
                    shutil.copyfileobj(spool, out, CHUNK)
                out.write(b"</tv>\n")
            log("Checking the complete gzip and XML, including every channel reference...")
            validate_merged(pending, len(channels), counts["programmes_written"])
            size = pending.stat().st_size
            pending.replace(output)
        finally:
            pending.unlink(missing_ok=True)
    return {"created_at_utc": datetime.now(timezone.utc).isoformat(), "source_index": INDEX,
            "output": str(output), "output_bytes": size, "feeds_merged": len(sources),
            "channels_written": len(channels), **counts,
            "channel_ids_with_merged_metadata": sorted(differing_channels),
            "deduplication": "Only identical programme content after XML indentation and attribute-order normalization is removed; differing entries are retained.",
            "sources": source_report}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=BASE / "all-guides.xml.gz")
    parser.add_argument("--work-dir", type=Path, default=BASE.parent / "work" / "epg-cache")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--cache-hours", type=float, default=6,
                        help="Reuse downloads this recent to resume interrupted runs; 0 forces fresh downloads")
    parser.add_argument("--countries", help="Optional comma-separated country codes; default downloads every feed")
    args = parser.parse_args(argv)
    if args.cache_hours < 0:
        parser.error("--cache-hours must be nonnegative")
    output = args.output.resolve()
    work = args.work_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    index = work / "guides.html"
    download(INDEX, index)
    urls = discover(index)
    if args.countries:
        wanted = {code.strip().lower() for code in args.countries.split(",") if code.strip()}
        available = {Path(urllib.parse.urlsplit(url).path).name[4:-7] for url in urls}
        if not wanted or wanted - available:
            parser.error(f"Unknown or empty country selection: {sorted(wanted - available)}")
        urls = [url for url in urls if Path(urllib.parse.urlsplit(url).path).name[4:-7] in wanted]
    log(f"Found {len(urls)} country feeds. Downloading with {args.workers} connections...")

    def get_feed(url):
        path = work / Path(urllib.parse.urlsplit(url).path).name
        cached = path.exists() and 0 <= time.time() - path.stat().st_mtime < args.cache_hours * 3600
        if not cached:
            download(url, path)
        return url, path, cached

    downloaded = {}
    failures = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(get_feed, url): url for url in urls}
        for index, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
                downloaded[result[0]] = result
                log(f"Downloaded {index}/{len(urls)}: {result[1].name} ({result[1].stat().st_size / 1048576:.1f} MiB){' [cached]' if result[2] else ''}")
            except Exception as error:
                failures.append(f"{futures[future]}: {error}")
                log(f"FAILED: {failures[-1]}")
    if failures:
        raise RuntimeError("Some feeds failed; existing output was not replaced. Rerun to resume.\n" + "\n".join(failures))
    report = merge([downloaded[url] for url in urls], output, work)
    report["scope"] = "all country feeds listed on the source page" if not args.countries else "selected countries"
    report_path = output.with_name(output.name.removesuffix(".xml.gz") + "-report.json")
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    log(f"Done: {output} ({report['output_bytes'] / 1048576:.1f} MiB), {report['feeds_merged']} feeds")
    log(f"Report: {report_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Rerun to reuse recent downloads.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
