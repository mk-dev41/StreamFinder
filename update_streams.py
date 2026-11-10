"""Add new streams from an archive.org account to streams.txt.

Lists the account's uploads (newest first), skips every upload that is already
in streams.txt, and appends one "M.D.YY, <direct .mp4 link>" line per new one.

Which file is picked from an upload:
  1. the longest Twitch recording  (name ends in "[<numeric id>]"), else
  2. the longest Kick recording    (name ends in "[Kick]"), else
  3. the longest other .mp4.
"_raw" copies are never used.

What is skipped:
  - uploads already linked in streams.txt,
  - re-uploads of a stream that is already listed (same Twitch id or file name),
  - older uploads whose date already has an entry (so history isn't duplicated).

Usage:
    python update_streams.py              # append new streams
    python update_streams.py --dry-run    # only print what would be added
    python update_streams.py --all        # scan the whole account, not just recent uploads

Standard library only, so it runs as-is in GitHub Actions.
"""

import argparse
import datetime
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ACCOUNT = "@pneuth"
STREAMS_FILE = Path(__file__).resolve().parent / "streams.txt"

LIST_URL = "https://archive.org/services/search/beta/page_production/"
METADATA_URL = "https://archive.org/metadata/"
USER_AGENT = "StreamFinder-list-updater/1.0"
PAGE_SIZE = 100
# Uploads are listed newest first, so once this many in a row are already
# in streams.txt we have caught up and can stop.
STOP_AFTER_KNOWN = 40
# A date that already has entries only gets another one if it is this close to
# the newest date in the list (e.g. a separate daytime Kick stream uploaded later).
SAME_DATE_WINDOW_DAYS = 7

TITLE_DATE = re.compile(r"^\s*\[(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")
LINE_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4}),", re.MULTILINE)
TWITCH_NAME = re.compile(r"\[(\d{6,})\]")
VIDEO_SUFFIXES = (".ia.mp4", ".mp4")  # order matters: longest suffix first


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def list_uploads(account, page):
    query = urllib.parse.urlencode({
        "user_query": "",
        "page_type": "account_details",
        "page_target": account,
        "page_elements": '["uploads"]',
        "hits_per_page": PAGE_SIZE,
        "page": page,
        "sort": "publicdate:desc",
    })
    data = fetch_json(f"{LIST_URL}?{query}")
    hits = data["response"]["body"]["page_elements"]["uploads"]["hits"]
    return [hit["fields"] for hit in hits["hits"]]


def to_date(year, month, day):
    try:
        return datetime.date(year if year >= 100 else 2000 + year, month, day)
    except ValueError:
        return None


def format_date(date):
    return f"{date.month}.{date.day}.{date.year % 100:02d}"


def stream_date(title, item_date):
    """Date of the stream: from a "[11.7.26, ...]" title, else the item's own date."""
    item = None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", item_date or "")
    if match:
        item = to_date(*(int(part) for part in match.groups()))

    match = TITLE_DATE.match(title or "")
    if match:
        month, day, year = (int(part) for part in match.groups())
        titled = to_date(year, month, day)
        # A title that disagrees with the item's own date by more than a few
        # days is a typo (usually the wrong year), so trust the item date.
        if titled and not (item and abs((titled - item).days) > 3):
            return titled
    return item


def existing_dates(text):
    dates = set()
    for month, day, year in LINE_DATE.findall(text):
        date = to_date(int(year), int(month), int(day))
        if date:
            dates.add(date)
    return dates


def pick_file(files):
    """Choose the one file to link from an item's file list.

    Returns (file name, stream key) or None. The key identifies the stream
    itself (Twitch id, or the recording's name), so re-uploads can be spotted.
    """
    recordings = {}  # name without suffix -> {"variants": {suffix: name}, "length": s, "size": b}
    for entry in files:
        name = entry.get("name", "")
        suffix = next((s for s in VIDEO_SUFFIXES if name.endswith(s)), None)
        if suffix is None:
            continue
        base = name[: -len(suffix)]
        if base.endswith("_raw"):
            continue
        recording = recordings.setdefault(base, {"variants": {}, "length": 0.0, "size": 0})
        recording["variants"][suffix] = name
        try:
            recording["length"] = max(recording["length"], float(entry.get("length") or 0))
            recording["size"] = max(recording["size"], int(entry.get("size") or 0))
        except ValueError:
            pass

    def longest(bases):
        return max(bases, key=lambda b: (recordings[b]["length"], recordings[b]["size"]))

    twitch = [b for b in recordings if TWITCH_NAME.search(b)]
    kick = [b for b in recordings if "[Kick]" in b]
    if twitch:
        base, preferred = longest(twitch), (".ia.mp4", ".mp4")
    elif kick:
        base, preferred = longest(kick), (".mp4", ".ia.mp4")
    elif recordings:
        base, preferred = longest(list(recordings)), (".ia.mp4", ".mp4")
    else:
        return None

    variants = recordings[base]["variants"]
    name = next(variants[suffix] for suffix in preferred if suffix in variants)
    twitch_id = TWITCH_NAME.search(base)
    return name, (twitch_id.group(1) if twitch_id else urllib.parse.quote(base))


def build_line(identifier, date, existing, used_keys):
    """Return (line, None) for an upload, or (None, reason) if it shouldn't be added."""
    item = fetch_json(METADATA_URL + urllib.parse.quote(identifier))
    picked = pick_file(item.get("files", []))
    if picked is None:
        return None, "no .mp4 yet"
    name, key = picked
    if key in existing or key in used_keys:
        return None, "re-upload of a stream already listed"
    server, directory = item.get("d1"), item.get("dir")
    if not server or not directory:
        return None, "no download server listed"
    used_keys.add(key)
    url = f"https://{server}{directory}/{urllib.parse.quote(name)}"
    return f"{format_date(date)}, {url}", None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print new lines without writing")
    parser.add_argument("--all", action="store_true", help="scan every upload on the account")
    parser.add_argument("--file", type=Path, default=STREAMS_FILE, help="path to streams.txt")
    args = parser.parse_args()

    existing = args.file.read_bytes().decode("utf-8") if args.file.exists() else ""
    listed_dates = existing_dates(existing)
    newest = max(listed_dates, default=datetime.date.min)
    window = datetime.timedelta(days=SAME_DATE_WINDOW_DAYS)

    new_lines, skipped, seen, used_keys = [], [], set(), set()
    known_in_a_row, page, caught_up = 0, 1, False
    while not caught_up:
        uploads = list_uploads(ACCOUNT, page)
        if not uploads:
            break
        for upload in uploads:
            identifier = upload["identifier"]
            if identifier in seen:
                continue
            seen.add(identifier)
            if f"/items/{identifier}/" in existing:
                known_in_a_row += 1
                if not args.all and known_in_a_row >= STOP_AFTER_KNOWN:
                    caught_up = True
                    break
                continue
            known_in_a_row = 0

            date = stream_date(upload.get("title", ""), upload.get("date", ""))
            if date is None:
                skipped.append(f"{identifier}: no usable date")
                continue
            if date in listed_dates and date - newest < -window:
                skipped.append(f"{identifier}: {format_date(date)} already has a stream")
                continue
            line, reason = build_line(identifier, date, existing, used_keys)
            if line is None:
                skipped.append(f"{identifier}: {reason}")
            else:
                new_lines.append((date, line))
            time.sleep(0.3)  # be polite to archive.org
        page += 1

    new_lines.sort(key=lambda pair: pair[0])
    for _, line in new_lines:
        print("+", line)
    for reason in skipped:
        print("skipped", reason)
    print(f"{len(new_lines)} new stream(s), {len(skipped)} skipped")

    if new_lines and not args.dry_run:
        newline = "\r\n" if "\r\n" in existing else "\n"
        with args.file.open("a", encoding="utf-8", newline="") as handle:
            if existing and not existing.endswith("\n"):
                handle.write(newline)
            for _, line in new_lines:
                handle.write(line + newline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
