#!/usr/bin/env python3
"""Update only ADCELL gross and strike-through prices from Shopify."""
import argparse
import csv
import decimal
import io
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import json
from collections import defaultdict

SHOP = "11vgxi-hm.myshopify.com"
CSV_FILE = pathlib.Path("508524-0.csv")
QUERY = """query($cursor: String) {
  productVariants(first: 250, after: $cursor) {
    nodes { id sku barcode price compareAtPrice }
    pageInfo { hasNextPage endCursor }
  }
}"""


def http_json(url, payload, headers=None):
    # OAuth expects URL-encoded form fields; GraphQL expects a JSON body.
    is_json = (headers or {}).get("Content-Type", "").lower() == "application/json"
    data = json.dumps(payload).encode("utf-8") if is_json else urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # OAuth error codes are useful for diagnosis, but never print credentials,
        # request headers, access tokens or an unfiltered response body.
        stage = "Shopify-Anmeldung" if url.endswith("/admin/oauth/access_token") else "Shopify-Produktabfrage"
        detail = ""
        if stage == "Shopify-Anmeldung":
            try:
                response = json.loads(error.read(4096).decode("utf-8"))
                known = {"invalid_client", "invalid_grant", "unauthorized_client", "unsupported_grant_type", "invalid_request"}
                code = response.get("error", "")
                if isinstance(code, str) and code in known:
                    detail = f" ({code})"
            except (ValueError, UnicodeError):
                pass
        raise RuntimeError(f"HTTP {error.code} bei {stage}{detail}") from error


def fetch_variants():
    client_id = os.environ.get("SHOPIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET fehlen")
    auth = http_json(f"https://{SHOP}/admin/oauth/access_token", {
        "grant_type": "client_credentials", "client_id": client_id,
        "client_secret": client_secret,
    })
    token = auth.get("access_token")
    if not token:
        raise RuntimeError("Shopify hat keinen Access Token geliefert")
    variants, cursor = [], None
    while True:
        result = http_json(f"https://{SHOP}/admin/api/2026-10/graphql.json",
                           {"query": QUERY, "variables": {"cursor": cursor}},
                           {"Content-Type": "application/json", "X-Shopify-Access-Token": token})
        if result.get("errors") or not result.get("data", {}).get("productVariants"):
            raise RuntimeError(f"Shopify GraphQL Fehler: {result.get('errors')}")
        connection = result["data"]["productVariants"]
        variants.extend(connection["nodes"])
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            break
        if not page.get("endCursor") or page["endCursor"] == cursor:
            raise RuntimeError("Ungültige Shopify-Paginierung")
        cursor = page["endCursor"]
    return variants


def unique_index(variants, field):
    grouped = defaultdict(list)
    for variant in variants:
        value = (variant.get(field) or "").strip()
        if value:
            grouped[value].append(variant)
    return {key: values[0] for key, values in grouped.items() if len(values) == 1}, {
        key for key, values in grouped.items() if len(values) > 1
    }


def eur(amount):
    value = decimal.Decimal(str(amount))
    if not value.is_finite() or value <= 0:
        raise ValueError("Ungültiger Shopify-Preis")
    return str(value.quantize(decimal.Decimal("0.01"))).replace(".", ",")


def update(csv_path, variants, minimum_match=0.75):
    original = csv_path.read_bytes()
    decoded = original.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(decoded, newline=""), delimiter=";")
    headers = reader.fieldnames
    required = {"Anbieter Artikelnummer AAN", "europäische Artikelnummer EAN", "Preis (Brutto)", "Streichpreis", "Deeplink"}
    if not headers or not required.issubset(headers):
        raise RuntimeError("Unerwartete ADCELL-CSV-Spalten")
    rows = list(reader)
    if not rows:
        raise RuntimeError("ADCELL-CSV ist leer")
    if any(None in row for row in rows):
        raise RuntimeError("ADCELL-CSV hat fehlerhafte Zeilen")
    sku_index, sku_duplicates = unique_index(variants, "sku")
    ean_index, ean_duplicates = unique_index(variants, "barcode")
    matched = 0
    changed = 0
    unmatched = []
    for row in rows:
        sku = row["Anbieter Artikelnummer AAN"].strip()
        ean = row["europäische Artikelnummer EAN"].strip()
        if (sku and sku in sku_duplicates) or (ean and ean in ean_duplicates):
            unmatched.append(sku or ean)
            continue
        by_sku = sku_index.get(sku) if sku else None
        by_ean = ean_index.get(ean) if ean else None
        if by_sku and by_ean and by_sku["id"] != by_ean["id"]:
            raise RuntimeError(f"SKU/EAN widersprechen sich bei AAN {sku}")
        variant = by_sku or by_ean
        if not variant:
            unmatched.append(sku or ean)
            continue
        matched += 1
        gross = eur(variant["price"])
        old = variant.get("compareAtPrice")
        strike = eur(old) if old and decimal.Decimal(str(old)) > decimal.Decimal(str(variant["price"])) else ""
        if row["Preis (Brutto)"] != gross or row["Streichpreis"] != strike:
            row["Preis (Brutto)"] = gross
            row["Streichpreis"] = strike
            changed += 1
    ratio = matched / len(rows)
    print(f"Shopify-Varianten: {len(variants)} | ADCELL-Zeilen: {len(rows)} | Zugeordnet: {matched} | Preisänderungen: {changed}")
    if unmatched:
        print(f"Nicht zugeordnet (erste 20): {unmatched[:20]}")
    if ratio < minimum_match:
        raise RuntimeError(f"Nur {ratio:.1%} zugeordnet. Mindestquote: {minimum_match:.0%}; Datei bleibt unverändert.")
    if changed == 0:
        return False
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_path.write_bytes(output.getvalue().encode("utf-8-sig"))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=pathlib.Path, default=CSV_FILE)
    args = parser.parse_args()
    changed = update(args.file, fetch_variants())
    print("ADCELL-Datei aktualisiert" if changed else "Keine Preisänderungen")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, decimal.InvalidOperation) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
