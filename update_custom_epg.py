#!/usr/bin/env python3
"""Build the small XMLTV guide used by the custom TiviMate playlist."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import html
import json
from pathlib import Path
import shutil
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE_URL = "https://iptv-epg.org/files/epg-{country}.xml.gz"
CHUNK = 1024 * 1024
TRANSLATION_URL = "https://api.mymemory.translated.net/get"
MAX_TRANSLATION_CHARS = 4_500
MAX_TRANSLATION_REQUESTS = 75


def xml_language(element: ET.Element) -> str:
    return (element.get("lang") or element.get("{http://www.w3.org/XML/1998/namespace}lang") or "").split("-", 1)[0].lower()


def translate_text(text: str, source_language: str) -> str | None:
    query = urllib.parse.urlencode({
        "q": text,
        "langpair": f"{source_language}|en",
        "mt": "1",
    })
    request = urllib.request.Request(
        f"{TRANSLATION_URL}?{query}",
        headers={"User-Agent": "TiviMate-Custom-EPG/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8, context=ssl.create_default_context()) as response:
            payload = json.load(response)
        if payload.get("responseStatus") != 200:
            return None
        translation = html.unescape((payload.get("responseData") or {}).get("translatedText") or "").strip()
        return translation or None
    except Exception:
        return None


def translate_programme_titles(
    programmes: dict[str, list[ET.Element]],
    cache_path: Path,
) -> tuple[int, int]:
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cache = {}
    if not isinstance(cache, dict):
        raise ValueError("translation cache must be a JSON object")

    translated_count = 0
    request_chars = 0
    request_count = 0
    dirty = False
    for listings in programmes.values():
        for programme in listings:
            for field in programme.findall("title") + programme.findall("sub-title"):
                source_language = xml_language(field)
                original = (field.text or "").strip()
                if not original or source_language in ("", "en"):
                    continue
                key = f"{source_language}|{original}"
                translation = cache.get(key)
                if not isinstance(translation, str):
                    # MyMemory accepts a maximum of 500 bytes per request; the
                    # anonymous tier is capped at 5,000 characters each day.
                    if (
                        len(original.encode("utf-8")) > 500
                        or request_chars + len(original) > MAX_TRANSLATION_CHARS
                        or request_count >= MAX_TRANSLATION_REQUESTS
                    ):
                        continue
                    translation = translate_text(original, source_language)
                    request_chars += len(original)
                    request_count += 1
                    if translation is None:
                        continue
                    cache[key] = translation
                    dirty = True
                field.text = translation
                field.set("lang", "en")
                field.attrib.pop("{http://www.w3.org/XML/1998/namespace}lang", None)
                translated_count += 1

    if dirty:
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return translated_count, request_chars




def download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "TiviMate-Custom-EPG/1.0"})
            with urllib.request.urlopen(request, timeout=120, context=ssl.create_default_context()) as response:
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output, CHUNK)
            with gzip.open(partial, "rb") as source:
                _, root = next(ET.iterparse(source, events=("start",)))
                if root.tag != "tv":
                    raise ValueError("download is not a gzipped XMLTV file")
            partial.replace(target)
            return
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def download_xmltv(url: str) -> ET.Element:
    request = urllib.request.Request(url, headers={"User-Agent": "TiviMate-Custom-EPG/1.0"})
    with urllib.request.urlopen(request, timeout=120, context=ssl.create_default_context()) as response:
        root = ET.fromstring(response.read())
    if root.tag != "tv":
        raise ValueError(f"external source is not XMLTV: {url}")
    return root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("custom-channels.json"))
    parser.add_argument("--output", type=Path, default=Path("all-channels-epg.xml.gz"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, help="Use existing epg-XX.xml.gz files for local testing")
    parser.add_argument("--translation-cache", type=Path, default=Path("translation-cache.json"))
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

    for source_config in config.get("external_xmltv", []):
        target_id = source_config["id"]
        if target_id not in programmes:
            raise ValueError(f"external source targets unknown guide ID: {target_id}")
        root = download_xmltv(source_config["url"])
        found = 0
        for element in root.findall("programme"):
            element.set("channel", target_id)
            encoded = ET.tostring(element, encoding="utf-8")
            digest = hashlib.sha256(encoded).digest()
            if digest not in seen_programmes:
                seen_programmes.add(digest)
                programmes[target_id].append(copy.deepcopy(element))
                found += 1
        if not found:
            raise ValueError(f"no programmes found in external source for {target_id}")
        print(f"Read external guide for {target_id}", flush=True)

    translated_count, translation_chars = translate_programme_titles(programmes, args.translation_cache)
    if translated_count:
        print(f"Translated {translated_count} title fields ({translation_chars} new request characters)", flush=True)

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
