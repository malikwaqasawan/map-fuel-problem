import io
import json
import zipfile

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

PLACES_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2025_Gazetteer/2025_Gaz_place_national.zip"
)
COUSUBS_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2025_Gazetteer/2025_Gaz_cousubs_national.zip"
)
DATA_DIR = settings.BASE_DIR / "routing" / "data"
PLACES_ZIP_PATH = DATA_DIR / "gazetteer_places_national.zip"
COUSUBS_ZIP_PATH = DATA_DIR / "gazetteer_cousubs_national.zip"
LOOKUP_PATH = DATA_DIR / "gazetteer_lookup.json"

# strip designation suffixes like "Abbeville city" / "Abanda CDP"; longest-first
PLACE_SUFFIXES = sorted(
    [
        "CDP", "city", "town", "village", "borough", "municipality",
        "corporation", "government", "County", "county", "comunidad",
        "zona urbana", "urbana", "(balance)",
    ],
    key=len,
    reverse=True,
)
COUSUB_SUFFIXES = sorted(
    ["township", "CCD", "town", "city", "borough", "barrio", "precinct", "village", "district", "UT"],
    key=len,
    reverse=True,
)


def strip_designation(name, suffixes):
    for suffix in suffixes:
        if name.endswith(" " + suffix):
            return name[: -(len(suffix) + 1)].strip()
    return name


def gazetteer_key(city, state):
    return f"{city.strip().upper()}|{state.strip().upper()}"


def _download(url, dest_path, skip_download, stdout):
    if skip_download and dest_path.exists():
        stdout.write(f"Reusing cached {dest_path}")
        return
    stdout.write(f"Downloading {url} ...")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    stdout.write(f"Saved {len(resp.content):,} bytes to {dest_path}")


def _parse_into(lookup, zip_path, suffixes, overwrite):
    """Parse a pipe-delimited Gazetteer zip into `lookup`. overwrite=False
    (County Subdivisions) only fills gaps, never clobbers a Places match."""
    added = 0
    with zipfile.ZipFile(zip_path) as zf:
        txt_name = next(n for n in zf.namelist() if n.endswith(".txt"))
        with zf.open(txt_name) as raw:
            text = io.TextIOWrapper(raw, encoding="latin-1")
            header = next(text).rstrip("\n").split("|")
            idx = {name: i for i, name in enumerate(header)}
            required = (idx["USPS"], idx["NAME"], idx["INTPTLAT"], idx["INTPTLONG"])
            for line in text:
                parts = line.rstrip("\n").split("|")
                if len(parts) <= max(required):
                    continue
                state = parts[idx["USPS"]].strip()
                name = strip_designation(parts[idx["NAME"]].strip(), suffixes)
                try:
                    lat = float(parts[idx["INTPTLAT"]])
                    lon = float(parts[idx["INTPTLONG"]])
                except ValueError:
                    continue
                key = gazetteer_key(name, state)
                if overwrite or key not in lookup:
                    lookup[key] = [lat, lon]
                    added += 1
    return added


class Command(BaseCommand):
    help = "Download the US Census Gazetteer files and build the (city, state) -> (lat, lon) coordinate lookup."

    def add_arguments(self, parser):
        parser.add_argument("--places-url", default=PLACES_URL)
        parser.add_argument("--cousubs-url", default=COUSUBS_URL)
        parser.add_argument(
            "--skip-download", action="store_true",
            help="Reuse already-downloaded zips under routing/data/ if present",
        )

    def handle(self, *args, **opts):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        _download(opts["places_url"], PLACES_ZIP_PATH, opts["skip_download"], self.stdout)
        _download(opts["cousubs_url"], COUSUBS_ZIP_PATH, opts["skip_download"], self.stdout)

        lookup = {}
        places_added = _parse_into(lookup, PLACES_ZIP_PATH, PLACE_SUFFIXES, overwrite=True)
        cousubs_added = _parse_into(lookup, COUSUBS_ZIP_PATH, COUSUB_SUFFIXES, overwrite=False)

        LOOKUP_PATH.write_text(json.dumps(lookup, sort_keys=True))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(lookup):,} place lookups to {LOOKUP_PATH} "
            f"({places_added:,} from Places, {cousubs_added:,} gap-fills from County Subdivisions)"
        ))
