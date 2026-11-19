"""Build a Pages artifact from the MySQL snapshot committed to this repository."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from urllib.request import urlopen
from urllib.error import HTTPError

SITE = Path(__file__).resolve().parents[1]
SOURCE = "https://ximengy127.github.io/index-basis-public/data/data.json"
SOURCES = {"Wind 日频", "米筐日频"}
GROUPS = Counter({prefix: 4 for prefix in ("IH", "IF", "IC", "IM")})


def load_payload(snapshot: Path | None) -> dict:
    if snapshot:
        with snapshot.open("rb") as stream:
            return json.load(stream)
    with urlopen(SOURCE, timeout=60) as response:
        return json.load(response)


def validate(payload: dict) -> None:
    if payload.get("schemaVersion") != 2 or not payload.get("rows"):
        raise ValueError("The source must provide a non-empty schema-v2 snapshot.")
    datetime.fromisoformat(payload["updatedAt"].replace("Z", "+00:00"))

    groups = defaultdict(Counter)
    keys = set()
    for row in payload["rows"]:
        day = date.fromisoformat(row["date"])
        key = day, row["contract"]
        if key in keys or row["dataSource"] not in SOURCES:
            raise ValueError(f"Duplicate contract or unsupported source: {key}.")
        keys.add(key)
        groups[day][row["prefix"]] += 1

    if any(counts != GROUPS for counts in groups.values()):
        raise ValueError("The source snapshot contains incomplete trading dates.")
    if max(groups).isoformat() != payload["sourceDate"]:
        raise ValueError("The source date does not match the latest trading date.")


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def write_chunk(directory: Path, stem: str, rows: list[dict]) -> str:
    content = json_bytes({"schemaVersion": 2, "rows": rows})
    filename = f"{stem}-{sha256(content).hexdigest()[:16]}.json"
    (directory / filename).write_bytes(content)
    return filename


def write_bundle(directory: Path, payload: dict) -> None:
    """Write the same manifest and chunks used by the manual MySQL export."""
    directory.mkdir(parents=True, exist_ok=True)
    cutoff = (date.fromisoformat(payload["sourceDate"]) - timedelta(days=59)).isoformat()
    recent = []
    history = defaultdict(list)
    for row in payload["rows"]:
        day = row["date"]
        if day >= cutoff:
            recent.append(row)
        else:
            history[day[:4]].append(row)

    files = [
        {"start": min(row["date"] for row in rows),
         "end": max(row["date"] for row in rows),
         "file": write_chunk(directory, year, rows)}
        for year, rows in sorted(history.items())
    ]
    manifest = {key: payload[key] for key in ("schemaVersion", "sourceDate", "updatedAt", "status")}
    manifest.update(
        firstDate=min(row["date"] for row in payload["rows"]),
        recentStart=min(row["date"] for row in recent),
        recentFile=write_chunk(directory, "recent", recent),
        history=files,
    )
    (directory / "manifest.json").write_bytes(json_bytes(manifest))


def deployment_signature(payload: dict) -> str:
    ignored = {"capturedAt", "periodDividendUpdatedAt"}
    values = {key: value for key, value in payload.items() if key not in ("updatedAt", "rows")}
    values["rows"] = sorted(
        ({key: value for key, value in row.items() if key not in ignored}
         for row in payload["rows"]),
        key=lambda row: (row["date"], row["contract"]),
    )
    digest = sha256(json_bytes(values))
    for name in ("index.html", "app.js", "styles.css", ".nojekyll", "api-config.js"):
        digest.update(name.encode())
        digest.update((SITE / name).read_bytes())
    return digest.hexdigest()


def current_signature(url: str) -> str | None:
    try:
        with urlopen(url, timeout=30) as response:
            return json.load(response).get("signature")
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, default=None,
                        help="Read the MySQL export stored in this repository.")
    parser.add_argument("--compare-url", help="Previously deployed signature URL")
    args = parser.parse_args()

    payload = load_payload(args.snapshot)
    validate(payload)
    signature = deployment_signature(payload)
    changed = not args.compare_url or current_signature(args.compare_url) != signature
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as handle:
            handle.write(f"changed={str(changed).lower()}\n")
    if not changed:
        print("Data and frontend match the deployed site; skipping deployment.")
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "app.js", "styles.css", ".nojekyll", "api-config.js"):
        shutil.copyfile(SITE / name, args.output_dir / name)
    write_bundle(args.output_dir / "data" / "fast", payload)
    (args.output_dir / "_deployment.json").write_bytes(json_bytes({"signature": signature}))
    print(f"Built {len(payload['rows']):,} rows, latest trading date {payload['sourceDate']}.")


if __name__ == "__main__":
    main()
