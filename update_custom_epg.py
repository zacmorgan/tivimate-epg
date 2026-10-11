#!/usr/bin/env python3
"""Build the small XMLTV guide used by the custom TiviMate playlist."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET

BASE_URL = "https://iptv-epg.org/files/epg-{country}.xml.gz"
CHUNK = 1024 * 1024


def download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "TiviMate-Custom-EPG/1.0"})
            with urllib.request.urlopen(request, timeout=120, context=ssl.create_default_context()) as response:
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output, CHUNK)
            with gzip.open(partial, "rb") as source:
                if source.read(5) != b"<?xml":
                    raise ValueError("download is not a gzipped XMLTV file")
            partial.replace(target)
            return
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("custom-channels.json"))
    parser.add_argument("--output", type=Path, default=Path("all-channels-epg.xml.gz"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, help="Use existing epg-XX.xml.gz files for local testing")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    channels = config["channels"]
    ids = {channel["id"] for channel in channels}
    if len(ids) != len(channels):
        raise ValueError("custom-channels.json contains duplicate IDs")
    args.work_dir.mkdir(parents=True, exist_ok=True)

    sources = []
    for country in config["countries"]:
        if args.source_dir:
            source = args.source_dir / f"epg-{country}.xml.gz"
        else:
            source = args.work_dir / f"epg-{country}.xml.gz"
            download(BASE_URL.format(country=country), source)
        sources.append(source)

    programmes = {channel_id: [] for channel_id in ids}
    seen_programmes = set()
    for source in sources:
        with gzip.open(source, "rb") as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag == "programme" and element.get("channel") in programmes:
                    encoded = ET.tostring(element, encoding="utf-8")
                    digest = hashlib.sha256(encoded).digest()
                    if digest not in seen_programmes:
                        seen_programmes.add(digest)
                        programmes[element.get("channel")].append(copy.deepcopy(element))
                if element.tag in ("channel", "programme"):
                    element.clear()
        print(f"Read {source.name}", flush=True)

    missing = [channel_id for channel_id, listings in programmes.items() if not listings]
    if missing:
        raise ValueError(f"No programmes found for {len(missing)} guide IDs: {missing}")

    root = ET.Element("tv", {
        "generator-info-name": "TiviMate custom playlist guide",
        "source-info-name": "IPTV-EPG.org",
        "source-info-url": "https://iptv-epg.org/guides",
    })
    for channel in channels:
        node = ET.SubElement(root, "channel", {"id": channel["id"]})
        ET.SubElement(node, "display-name").text = channel["name"]
        if channel.get("logo"):
            ET.SubElement(node, "icon", {"src": channel["logo"]})
    for channel in channels:
        for programme in programmes[channel["id"]]:
            root.append(programme)
    ET.indent(root, space="  ")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix=args.output.name + ".", suffix=".tmp", delete=False) as raw:
        pending = Path(raw.name)
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as zipped:
            ET.ElementTree(root).write(zipped, encoding="utf-8", xml_declaration=True)
    try:
        # Parse the complete pending output before replacing the working guide.
        with gzip.open(pending, "rb") as stream:
            parsed = ET.parse(stream).getroot()
        if len(parsed.findall("channel")) != len(channels):
            raise ValueError("output channel validation failed")
        if not parsed.findall("programme"):
            raise ValueError("output contains no programmes")
        pending.replace(args.output)
    finally:
        pending.unlink(missing_ok=True)

    print(f"Wrote {len(channels)} channels and {len(seen_programmes)} programmes to {args.output}")


if __name__ == "__main__":
    main()
