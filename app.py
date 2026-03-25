from flask import Flask, render_template, request, jsonify
import csv
import io
import re

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/geocode", methods=["POST"])
def geocode():
    """Parse pasted spreadsheet data and return structured property list."""
    raw = request.json.get("data", "")
    rows = _parse_tabular(raw)
    return jsonify(rows)


def _parse_tabular(text: str) -> list[dict]:
    """Parse tab- or comma-separated text (copied from Excel) into dicts.

    Expected columns (header row required, order flexible):
      name, address, city, state, zip, type, sf (or sqft), lat, lon (or lng/longitude)

    Lat/lon are required for plotting.
    """
    text = text.strip()
    if not text:
        return []

    # Detect delimiter
    first_line = text.split("\n")[0]
    if "\t" in first_line:
        delimiter = "\t"
    else:
        delimiter = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    # Normalize headers
    reader.fieldnames = [_norm(h) for h in reader.fieldnames]

    results = []
    for row in reader:
        entry = {}
        entry["name"] = _get(row, ["name", "property", "property_name", "propertyname"])
        entry["address"] = _get(row, ["address", "street", "street_address"])
        entry["city"] = _get(row, ["city"])
        entry["state"] = _get(row, ["state", "st"])
        entry["zip"] = _get(row, ["zip", "zipcode", "zip_code", "postal"])
        entry["type"] = _get(row, ["type", "property_type", "propertytype", "asset_type", "assettype", "use"])
        entry["sf"] = _float(_get(row, ["sf", "sqft", "sq_ft", "square_feet", "squarefeet", "size", "nra", "gla"]))
        entry["lat"] = _float(_get(row, ["lat", "latitude"]))
        entry["lon"] = _float(_get(row, ["lon", "lng", "longitude", "long"]))
        entry["units"] = _float(_get(row, ["units", "unit_count", "num_units"]))
        entry["year_built"] = _get(row, ["year_built", "yearbuilt", "built", "vintage"])
        entry["notes"] = _get(row, ["notes", "comments", "description"])
        results.append(entry)
    return results


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", header.strip().lower()).strip("_")


def _get(row: dict, keys: list[str]) -> str:
    for k in keys:
        if k in row and row[k]:
            return row[k].strip()
    return ""


def _float(val: str):
    if not val:
        return None
    val = val.replace(",", "").replace(" ", "")
    try:
        return float(val)
    except ValueError:
        return None


if __name__ == "__main__":
    import threading
    import webbrowser

    def open_browser():
        webbrowser.open("http://localhost:5050")

    threading.Timer(1.5, open_browser).start()
    app.run(debug=False, port=5050)
