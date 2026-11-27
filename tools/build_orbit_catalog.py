"""Build the evidence diagram's comparison sample from downloaded catalog snapshots."""

import argparse
import csv
import json
import re
import string
from datetime import date
from html.parser import HTMLParser
from pathlib import Path


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag == "td":
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)


def unpack(des):
    alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
    if len(des) == 5 and des[0].isalpha():
        return str(alphabet.index(des[0]) * 10000 + int(des[1:]))
    if len(des) == 7 and des[0].isalpha():
        year = alphabet.index(des[0]) * 100 + int(des[1:3])
        cycle = alphabet.index(des[4]) * 10 + int(des[5])
        return f"{year}{des[3]}{des[6]}" + (str(cycle) if cycle else "")
    return des


def build(folder):
    jpl = json.loads((folder / "sbdb.json").read_text())
    aliases = {}
    for values in jpl["data"]:
        row = dict(zip(jpl["fields"], values, strict=True))
        for alias in [row["pdes"], row["name"], *re.findall(r"\(([^)]+)\)", row["full_name"])]:
            if alias:
                aliases[alias.replace(" ", "")] = row
    catalog = {}

    def body(key):
        row = aliases[key]
        if row["pdes"] not in catalog:
            names = re.findall(r"\(([^)]+)\)", row["full_name"])
            item = {"name": row["name"] or (names[0] if names else row["pdes"])}
            for k in ["epoch", "a", "e", "i", "om", "w", "ma", "n"]:
                item[k] = float(row[k])
            catalog[row["pdes"]] = item
        return catalog[row["pdes"]]

    table = TableRows()
    table.feed((folder / "brown.html").read_text())
    ranked = [r for r in table.rows if r[0].isdigit() and len(r) >= 9 and r[7] != "centaur"]
    for rank, row in enumerate(ranked[:100], 1):
        entry = body(row[2] if row[2] in aliases else row[1])
        entry.update(rank=rank, diameter_km=int(re.match(r"\d+", row[3])[0]))
    with (folder / "detached.csv").open(encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            if row["type"].strip() == "detached":
                body(unpack(row["des"]))["detached"] = row["soi"].strip()

    # JPL Table 1 gives a,e,I,L,longitude of perihelion,node and their rates per century.
    page = (folder / "planets.html").read_text().split("<h4>Table 1</h4>", 1)[1]
    lines = page.split("<pre>", 1)[1].split("</pre>", 1)[0].splitlines()
    planets = []
    for i, line in enumerate(lines):
        match = re.match(
            r"^(Mercury|Venus|EM Bary|Mars|Jupiter|Saturn|Uranus|Neptune)\s+(.*)$", line
        )
        if match and not match[2].startswith("="):
            planets.append(
                dict(
                    name=match[1].replace("EM Bary", "Earth"),
                    base=list(map(float, match[2].split())),
                    rates=list(map(float, lines[i + 1].split())),
                )
            )
    assert len(planets) == 8 and len([r for r in catalog.values() if "rank" in r]) == 100
    return dict(as_of=date.today().isoformat(), planets=planets, kbos=list(catalog.values()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="directory with brown.html, detached.csv, sbdb.json and planets.html",
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(build(args.input), separators=(",", ":"), allow_nan=False) + "\n"
    )
