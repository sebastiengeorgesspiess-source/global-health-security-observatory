#!/usr/bin/env python3
import json
import hashlib
import os
import re
import statistics
import threading
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, abort

BASE = Path(__file__).resolve().parent
CACHE = BASE / "data" / "health-security.json"
HEAT = Path("/var/www/worldmonitor/hfo/raw_data/climate/cckp_cmip6_hd35.json")
RELEASE = BASE / "data" / "release.json"
METHODS = BASE / "data" / "methods.md"
app = Flask(__name__)
lock = threading.Lock()

INDICATORS = {
    "measles": ("SH.IMM.MEAS", "Measles immunization", "% of children 12–23 months"),
    "dtp3": ("SH.IMM.IDPT", "DTP3 immunization", "% of children 12–23 months"),
    "beds": ("SH.MED.BEDS.ZS", "Hospital beds", "per 1,000 people"),
    "health_spend": ("SH.XPD.CHEX.GD.ZS", "Current health expenditure", "% of GDP"),
    "physicians": ("SH.MED.PHYS.ZS", "Physicians", "per 1,000 people"),
    "uhc": ("SH_UHC_SCI", "Universal health coverage service index", "index 0–100"),
    "sanitation": ("SH.STA.SMSS.ZS", "Safely managed sanitation", "% of population"),
    "pm25": ("EN.ATM.PM25.MC.M3", "PM2.5 exposure", "µg/m³"),
    "urban": ("SP.URB.TOTL.IN.ZS", "Urban population", "% of population"),
    "travel": ("ST.INT.ARVL", "International tourism arrivals", "arrivals/year"),
    "tb": ("SH.TBS.INCD", "Tuberculosis incidence", "per 100,000 people/year"),
    "malaria": ("SH.MLR.INCD.P3", "Malaria incidence", "per 1,000 population at risk/year"),
    "u5mort": ("SH.DYN.MORT", "Under-five mortality", "per 1,000 live births"),
    "communicable_deaths": ("SH.DTH.COMM.ZS", "Deaths from communicable, maternal, perinatal and nutritional conditions", "% of deaths"),
    "age65": ("SP.POP.65UP.TO.ZS", "Population aged 65 and above", "% of population"),
    "population": ("SP.POP.TOTL", "Total population", "people"),
}

INDICATOR_KIND = {
    "tb": "reported/modelled disease burden", "malaria": "reported/modelled disease burden",
    "u5mort": "modelled outcome estimate", "communicable_deaths": "modelled cause-of-death estimate",
    "heat2050": "climate projection", "travel": "reported contextual exposure",
}


def clean_html(value):
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def get_json(url, timeout=40):
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "Sebastien-Spiess-Health-Observatory/1.0"})
    response.raise_for_status()
    return response.json()


def fetch_countries():
    payload = get_json("https://api.worldbank.org/v2/country?format=json&per_page=400")
    countries = {}
    for row in payload[1]:
        if not row.get("capitalCity") or not row.get("latitude") or not row.get("longitude"):
            continue
        if row.get("region", {}).get("value") == "Aggregates":
            continue
        countries[row["id"]] = {
            "iso3": row["id"], "iso2": row.get("iso2Code"), "name": row["name"],
            "region": row.get("region", {}).get("value"),
            "income": row.get("incomeLevel", {}).get("value"),
            "lat": float(row["latitude"]), "lon": float(row["longitude"]), "values": {}
        }
    return countries


def fetch_indicator(code):
    url = f"https://api.worldbank.org/v2/country/all/indicator/{code}?format=json&per_page=20000&date=2010:2025"
    payload = get_json(url, 55)
    latest, timelines = {}, {}
    for row in payload[1] if len(payload) > 1 else []:
        iso3, value = row.get("countryiso3code"), row.get("value")
        if not iso3 or value is None:
            continue
        year = int(row["date"])
        timelines.setdefault(iso3, {})[str(year)] = round(float(value), 2)
        if iso3 not in latest or year > latest[iso3]["year"]:
            latest[iso3] = {"value": round(float(value), 2), "year": year}
    for iso3, item in latest.items():
        item["timeline"] = dict(sorted(timelines.get(iso3, {}).items()))
    return latest


def add_heat_context(countries):
    if not HEAT.exists():
        return 0
    heat = json.loads(HEAT.read_text(encoding="utf-8"))["countries"]
    count = 0
    for iso3, country in countries.items():
        value = heat.get(iso3, {}).get("scenarios", {}).get("ssp245", {}).get("2040-2059")
        if value:
            country["values"]["heat2050"] = {"value": value["median"], "year": 2050,
                                                  "p10": value["p10"], "p90": value["p90"]}
            count += 1
    return count


def fetch_who_notices():
    payload = get_json("https://www.who.int/api/news/diseaseoutbreaknews?%24top=40&%24orderby=PublicationDate%20desc", 55)
    notices = []
    for row in payload.get("value", []):
        date = (row.get("PublicationDateAndTime") or row.get("PublicationDate") or "")[:10]
        if not date or date < "2022-01-01":
            continue
        summary = clean_html(row.get("Summary")) or clean_html(row.get("Overview"))
        notices.append({
            "date": date, "title": row.get("OverrideTitle") or row.get("Title") or "WHO outbreak notice",
            "summary": summary[:360] + ("…" if len(summary) > 360 else ""),
            "url": "https://www.who.int/emergencies/disease-outbreak-news/item/" + row.get("UrlName", ""),
            "source": "WHO Disease Outbreak News"
        })
    notices.sort(key=lambda item: item["date"], reverse=True)
    return notices[:30]


def median(values):
    return round(statistics.median(values), 1) if values else None


def build_payload():
    countries = fetch_countries()
    source_state = []
    for key, (code, label, unit) in INDICATORS.items():
        values = fetch_indicator(code)
        for iso3, item in values.items():
            if iso3 in countries:
                countries[iso3]["values"][key] = item
        source_state.append({"id": code, "label": label, "unit": unit, "records": len(values), "status": "online"})
    heat_records = add_heat_context(countries)
    source_state.append({"id": "CCKP.CMIP6.HD35.SSP245.2050", "label": "Extreme heat days",
                         "unit": "days/year", "records": heat_records, "status": "versioned local artifact"})
    notices = fetch_who_notices()
    country_rows = list(countries.values())
    observed_keys = list(INDICATORS)
    for country in country_rows:
        available = [country["values"][key] for key in observed_keys if key in country["values"]]
        years = [item["year"] for item in available]
        newest = max(years) if years else None
        stale = sum(1 for year in years if year < datetime.now(timezone.utc).year - 5)
        country["evidence_quality"] = {
            "available_indicators": len(available), "expected_indicators": len(observed_keys),
            "completeness_percent": round(100 * len(available) / len(observed_keys)),
            "newest_reference_year": newest, "indicators_older_than_five_years": stale,
            "interpretation": "Data availability, not health performance",
        }
    latest_year = datetime.now(timezone.utc).year
    recent = [n for n in notices if int(n["date"][:4]) >= latest_year - 1]
    summary = {
        "who_notices": len(recent),
        "countries": len(country_rows),
        "measles_median": median([c["values"]["measles"]["value"] for c in country_rows if "measles" in c["values"]]),
        "dtp3_median": median([c["values"]["dtp3"]["value"] for c in country_rows if "dtp3" in c["values"]]),
        "coverage": sum(1 for c in country_rows if c["values"]),
        "amr_glass_enrolled": 141,
        "amr_reporting_2023": 104,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification": "Official observed indicators and official outbreak notices",
        "summary": summary, "countries": country_rows, "notices": notices,
        "indicators": source_state,
        "framework": {
            "dimensions": ["verified threat signal", "population exposure", "population vulnerability", "system capacity", "evidence confidence"],
            "aggregation": "No composite score; dimensions remain separate to avoid compensatory weighting and false precision",
            "event_boundary": "WHO Disease Outbreak News are official event assessments, not incidence counts",
            "causal_boundary": "Spatial or temporal co-occurrence is hypothesis-generating and cannot establish causation",
        },
        "surveillance_gaps": [
            {"domain": "Animal and wildlife events", "status": "not integrated", "reference": "https://wahis.woah.org/", "reason": "WOAH WAHIS remains the authoritative consultation surface; absence here is not absence of animal disease"},
            {"domain": "Pathogen genomics", "status": "not integrated", "reference": "https://www.who.int/initiatives/genomic-surveillance-strategy", "reason": "No globally comparable open country feed is joined; sequence volume is not prevalence"},
            {"domain": "Wastewater surveillance", "status": "not integrated", "reference": "https://www.who.int/publications/m/item/wastewater-and-environmental-surveillance-for-one-or-more-pathogens--guidance-on-prioritization--implementation-and-integration", "reason": "Coverage and targets are heterogeneous; wastewater signals cannot be converted directly to cases"},
        ],
        "amr": {"glass_enrolled": 141, "reporting_countries_2023": 104,
                "confirmed_cases": "23+ million", "period": "2016–2023",
                "scope": "WHO GLASS surveillance; reporting coverage and representativeness vary by country",
                "source": "https://www.who.int/publications/i/item/B09585"},
        "sources": [
            {"name": "WHO Disease Outbreak News", "url": "https://www.who.int/emergencies/disease-outbreak-news", "role": "Official outbreak notices"},
            {"name": "World Bank Indicators API", "url": "https://api.worldbank.org/", "role": "Country health-system and immunization context"},
            {"name": "WHO Global Health Observatory", "url": "https://www.who.int/data/gho", "role": "Indicator definitions and global health evidence"}
            ,{"name": "WHO GLASS AMR Dashboard", "url": "https://data.who.int/dashboards/amr", "role": "Official AMR and antimicrobial-use surveillance context"}
            ,{"name": "World Bank CCKP CMIP6", "url": "https://climateknowledgeportal.worldbank.org/", "role": "Extreme-heat scenario context; not disease suitability"}
        ],
        "method_note": "Indicators retain their own reference years and original denominators. Missing observations remain missing. Disease-burden estimates are not case notifications. Mobility, urbanization, demography, air quality and climate are contextual hypothesis layers—not evidence of transmission, outbreak probability or causality."
    }


def refresh():
    with lock:
        payload = build_payload()
        if len(payload["countries"]) < 180 or len(payload["notices"]) < 1:
            raise ValueError("Scientific release gate failed: insufficient country or WHO-notice coverage")
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, CACHE)
        return payload


def load():
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return refresh()


@app.get("/")
def index():
    html = render_template("index.html")
    old = '<header class="ss-ribbon"><a class="ss-brand" href="/"><i></i><strong>SEBASTIEN SPIESS</strong><span>DATA OBSERVATORIES</span></a><nav><a href="/">INDEX</a><a href="/monitor/">OSINT</a><a href="/nexus/">NEXUS</a><a href="/hfo/">FUTURES</a><a href="/climate/">CLIMATE</a><a class="active" href="/health/">HEALTH</a></nav><span class="ss-section">BIOSECURITY</span></header>'
    new = '<div class="ss-ribbon"><div class="ss-id"><b>SEBASTIEN SPIESS</b><span>DATA OBSERVATORIES</span></div><nav class="ss-links"><a href="/">Index</a><a href="/monitor/">OSINT</a><a href="/nexus/">Nexus</a><a href="/hfo/">Futures</a><a href="/climate/">Climate</a><a href="/health/" aria-current="page">Health</a></nav><div class="ss-pagecode">SYS/05</div></div>'
    return html.replace(old, new).replace('<div class="hero-actions">', '<div class="hero-actions"><a class="btn" href="/">← BACK TO INDEX</a>', 1).replace('RELEASE 2.0', 'RELEASE 3.1 · DOI').replace('href="/health/api/release"', 'href="https://doi.org/10.5281/zenodo.22176516" target="_blank" rel="noopener"').replace('<a class="btn" href="/health/api/evidence">EVIDENCE JSON ↓</a>', '<a class="btn" href="/health/methods/">METHODS PAPER ↗</a><a class="btn" href="/health/api/evidence">EVIDENCE JSON ↓</a>', 1)


@app.get("/methods/")
def methods_page():
    return render_template("methods.html")


@app.get("/api/data")
def data():
    return jsonify(load())


@app.post("/api/refresh")
def api_refresh():
    if lock.locked():
        return jsonify({"status": "busy"}), 409
    try:
        return jsonify(refresh())
    except Exception as error:
        return jsonify({"status": "degraded", "error": str(error), "cached": CACHE.exists()}), 502


@app.get("/api/health")
def health():
    payload = load()
    return jsonify({"status": "ok", "updated_at": payload["generated_at"], "countries": len(payload["countries"]), "notices": len(payload["notices"])})


@app.get("/api/export.json")
def export():
    return jsonify(load())


@app.get("/api/country/<iso3>.json")
def country_json(iso3):
    country = next((c for c in load()["countries"] if c["iso3"] == iso3.upper()), None)
    if not country:
        abort(404)
    return jsonify(country)


@app.get("/api/country/<iso3>.csv")
def country_csv(iso3):
    country = next((c for c in load()["countries"] if c["iso3"] == iso3.upper()), None)
    if not country:
        abort(404)
    rows = ["indicator,value,year,unit"]
    units = {key: spec[2] for key, spec in INDICATORS.items()}
    units["heat2050"] = "days/year"
    for key, value in sorted(country["values"].items()):
        rows.append(f'{key},{value.get("value", "")},{value.get("year", "")},{units.get(key, "")}')
    return Response("\n".join(rows) + "\n", mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="health_{iso3.upper()}.csv"'})


@app.get("/api/evidence")
def evidence():
    payload = load()
    artifact_hash = hashlib.sha256(CACHE.read_bytes()).hexdigest() if CACHE.exists() else None
    registry = []
    for item in payload["indicators"]:
        key = next((key for key, spec in INDICATORS.items() if spec[0] == item["id"]), "heat2050")
        registry.append({**item, "classification": INDICATOR_KIND.get(key, "reported/modelled official indicator"),
                         "temporal_coverage_requested": "2010–2025" if key != "heat2050" else "2040–2059 midpoint labelled 2050",
                         "spatial_resolution": "country/territory", "missing_values": "retained; never zero-filled",
                         "transformation": "latest non-missing value plus country-year timeline; rounded to 2 decimals" if key != "heat2050" else "multi-model median with P10–P90 retained",
                         "source_url": "https://api.worldbank.org/" if key != "heat2050" else "https://climateknowledgeportal.worldbank.org/"})
    return jsonify({"schema": "one-health-evidence-2.0", "generated_at": payload["generated_at"],
                    "artifact": {"path": "health-security.json", "sha256": artifact_hash},
                    "principles": ["No outbreak prediction", "No causal inference from spatial overlap",
                                   "Missing observations are not zero", "Reference years and denominators remain visible",
                                   "No composite risk score"],
                    "framework": payload.get("framework"), "surveillance_gaps": payload.get("surveillance_gaps"),
                    "indicators": registry, "sources": payload["sources"]})


@app.get("/api/release")
def release():
    if not RELEASE.exists():
        abort(503)
    return jsonify(json.loads(RELEASE.read_text(encoding="utf-8")))


@app.get("/api/citation.bib")
def citation():
    return Response("""@software{SpiessHealth2026,\n  author={Spiess, Sebastien},\n  title={Global Pathogen and Health Security Observatory},\n  year={2026},\n  version={3.1.0},\n  doi={10.5281/zenodo.22176516},\n  url={https://doi.org/10.5281/zenodo.22176516},\n  note={Research-grade exploratory observatory; archived release 3.1.0}\n}\n""", mimetype="application/x-bibtex")


@app.get("/api/methods.md")
def methods_markdown():
    if not METHODS.exists():
        abort(503)
    return Response(METHODS.read_text(encoding="utf-8"), mimetype="text/markdown",
                    headers={"Content-Disposition": 'attachment; filename="health-observatory-methods-v3.1.md"'})


@app.get("/api/zenodo.json")
def zenodo_metadata():
    return jsonify({
        "metadata": {
            "title": "Global Pathogen & Health Security Observatory",
            "upload_type": "software", "publication_date": "2026-08-29",
            "description": "Research-grade exploratory observatory combining official outbreak intelligence, country health indicators and climate context with explicit evidence boundaries.",
            "creators": [{"name": "Spiess, Sebastien"}],
            "version": "3.1.0", "language": "eng",
            "keywords": ["One Health", "public health surveillance", "health security", "data visualization", "reproducible research"],
            "license": "other-open", "related_identifiers": [{"identifier": "https://sebastienspiess.ch/health/", "relation": "isSupplementTo", "scheme": "url"}],
            "doi": "10.5281/zenodo.22176516",
            "notes": "Published version-specific Zenodo release; source-dataset terms remain applicable."
        },
        "doi": "10.5281/zenodo.22176516", "doi_status": "published"
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8092)
