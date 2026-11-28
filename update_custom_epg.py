#!/usr/bin/env python3
"""Build the small XMLTV guide used by the custom TiviMate playlist."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
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
    now: datetime | None = None,
) -> tuple[int, int, int, int]:
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cache = {}
    if not isinstance(cache, dict):
        raise ValueError("translation cache must be a JSON object")

    translated_count = 0
    translated_descriptions = 0
    preferred_english_titles = 0
    request_chars = 0
    request_count = 0
    dirty = False

    # These show titles were mistranslated by the public service. The English
    # names below match the titles supplied by the Mexican source guide.
    for key, correct_title in {
        "es|El novato": "The Rookie",
        "es|La ley y el orden": "Law and Order",
        "es|La ley y el orden: UVE": "Law and Order: Special Victims Unit",
    }.items():
        if cache.get(key) != correct_title:
            cache[key] = correct_title
            dirty = True

    def translate_field(field: ET.Element, *, reject_unchanged: bool = False) -> bool:
        nonlocal request_chars, request_count, dirty
        source_language = xml_language(field)
        original = (field.text or "").strip()
        if not original or source_language in ("", "en"):
            return False
        key = f"{source_language}|{original}"
        translation = cache.get(key)
        if not isinstance(translation, str) or not translation.strip():
            # MyMemory accepts a maximum of 500 bytes per request; the
            # anonymous tier is capped at 5,000 characters each day.
            if (
                len(original.encode("utf-8")) > 500
                or request_chars + len(original) > MAX_TRANSLATION_CHARS
                or request_count >= MAX_TRANSLATION_REQUESTS
            ):
                return False
            translation = translate_text(original, source_language)
            request_chars += len(original)
            request_count += 1
            if translation is None:
                return False
            if not reject_unchanged or translation.casefold() != original.casefold():
                cache[key] = translation
                dirty = True
        if reject_unchanged and translation.casefold() == original.casefold():
            return False
        field.text = translation
        field.set("lang", "en")
        field.attrib.pop("{http://www.w3.org/XML/1998/namespace}lang", None)
        return True

    def prefer_native_english(programme: ET.Element, tag: str) -> bool:
        nonlocal preferred_english_titles
        fields = programme.findall(tag)
        english = next((field for field in fields if xml_language(field) == "en" and (field.text or "").strip()), None)
        if english is None:
            return False
        if fields[0] is not english:
            position = list(programme).index(fields[0])
            programme.remove(english)
            programme.insert(position, english)
            if tag in ("title", "sub-title"):
                preferred_english_titles += 1
        return True

    # Mexican listings often have an official English title as well as a
    # Spanish title. Prefer the official title over machine translation.
    for channel_id, listings in programmes.items():
        for programme in listings:
            for tag in ("title", "sub-title"):
                if channel_id.endswith(".mx") and prefer_native_english(programme, tag):
                    continue
                for field in programme.findall(tag):
                    if translate_field(field, reject_unchanged=channel_id.endswith(".mx")):
                        translated_count += 1

    # Spend any remaining free-translation allowance on the closest current or
    # upcoming Mexican descriptions, interleaved by broadcast time.
    now = now or datetime.now(timezone.utc)

    def programme_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y%m%d%H%M%S %z")
        except ValueError:
            return None

    upcoming = []
    for channel_id, listings in programmes.items():
        if not channel_id.endswith(".mx"):
            continue
        for programme in listings:
            stop = programme_time(programme.get("stop"))
            if stop is not None and stop <= now:
                continue
            start = programme_time(programme.get("start")) or datetime.max.replace(tzinfo=timezone.utc)
            upcoming.append((start, channel_id, programme))
    upcoming.sort(key=lambda item: (item[0], item[1]))
    for _, _, programme in upcoming:
        if prefer_native_english(programme, "desc"):
            continue
        for field in programme.findall("desc"):
            if xml_language(field) == "es" and translate_field(field, reject_unchanged=True):
                translated_descriptions += 1

    if dirty:
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return translated_count, translated_descriptions, preferred_english_titles, request_chars




def download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    compressed = target.suffix == ".gz"
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "TiviMate-Custom-EPG/1.0"})
            timeout = 120 if compressed else 30
            with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output, CHUNK)
            if compressed:
                with gzip.open(partial, "rb") as source:
                    _, root = next(ET.iterparse(source, events=("start",)))
            else:
                root = ET.parse(partial).getroot()
                if root.find("programme") is None:
                    raise ValueError(f"No programme listings in {target.name}")
            if root.tag != "tv":
                raise ValueError(f"{target.name} is not XMLTV")
            partial.replace(target)
            return
        except Exception as error:
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            print(f"Retrying {target.name} after {error}", flush=True)
            time.sleep(2 ** (attempt + 1))


def shift_xmltv_timestamp(value: str, offset_minutes: int) -> str:
    """Shift an XMLTV timestamp while preserving its original UTC offset."""
    if len(value) < 14 or not value[:14].isdigit():
        raise ValueError(f"Unrecognized XMLTV timestamp: {value!r}")
    shifted = datetime.strptime(value[:14], "%Y%m%d%H%M%S") + timedelta(minutes=offset_minutes)
    return shifted.strftime("%Y%m%d%H%M%S") + value[14:]


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

    alternate_ids = {ch["regional_alias"] for ch in channels if ch.get("regional_alias")}
    regional_programmes = {channel_id: [] for channel_id in ids | alternate_ids}
    seen_programmes = set()
    for source in sources:
        with gzip.open(source, "rb") as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag == "programme" and element.get("channel") in regional_programmes:
                    encoded = ET.tostring(element, encoding="utf-8")
                    digest = hashlib.sha256(encoded).digest()
                    if digest not in seen_programmes:
                        seen_programmes.add(digest)
                        regional_programmes[element.get("channel")].append(copy.deepcopy(element))
                if element.tag in ("channel", "programme"):
                    element.clear()
        print(f"Read {source.name}", flush=True)

    # Some stations have a different ID in the regional guide. Keep the M3U ID
    # in the output and contact the dedicated feed only if both IDs are absent.
    programmes = {channel_id: regional_programmes[channel_id] for channel_id in ids}
    for channel in channels:
        channel_id = channel["id"]
        if programmes[channel_id]:
            continue
        alias = channel.get("regional_alias")
        if alias and regional_programmes[alias]:
            listings = regional_programmes[alias]
            print(f"Using regional guide {alias} for {channel_id}", flush=True)
        elif channel.get("fallback_xml_url"):
            fallback_file = args.work_dir / "fallback.xml"
            download(channel["fallback_xml_url"], fallback_file)
            listings = ET.parse(fallback_file).getroot().findall("programme")
            print(f"Read fallback guide for {channel_id}", flush=True)
        else:
            continue
        for programme in listings:
            copied = copy.deepcopy(programme)
            copied.set("channel", channel_id)
            programmes[channel_id].append(copied)

    # When no dedicated West listings are available, time-shift the matching
    # East feed by three hours. Keep separate XMLTV IDs for East and West.
    for channel in channels:
        channel_id = channel["id"]
        source_id = channel.get("copy_programmes_from")
        if not source_id or programmes[channel_id]:
            continue  # Prefer a real West feed if the upstream guide has one.
        if source_id not in programmes or not programmes[source_id]:
            raise ValueError(f"No programmes found for source {source_id} of {channel_id}")
        offset_minutes = int(channel.get("offset_minutes", 0))
        for source_programme in programmes[source_id]:
            copied = copy.deepcopy(source_programme)
            copied.set("channel", channel_id)
            for time_attr in ("start", "stop"):
                value = copied.get(time_attr)
                if value:
                    copied.set(time_attr, shift_xmltv_timestamp(value, offset_minutes))
            programmes[channel_id].append(copied)
        print(f"Built {channel_id} from {source_id} with {offset_minutes:+} minute shift", flush=True)

    title_count, description_count, preferred_titles, translation_chars = translate_programme_titles(programmes, args.translation_cache)
    print(
        f"Translated {title_count} title fields and {description_count} Mexican descriptions; "
        f"preferred {preferred_titles} existing English titles "
        f"({translation_chars} new request characters)",
        flush=True,
    )

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

    print(f"Wrote {len(channels)} channels and {sum(map(len, programmes.values()))} programmes to {args.output}")


if __name__ == "__main__":
    main()

