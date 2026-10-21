"""Flask API and static-file server for the React GTFS web application."""

import time
from datetime import datetime
from pathlib import Path
from threading import Lock

from flask import Flask, Response, jsonify, request, send_from_directory
import requests

import gtfs_rt_prouhon as transit

app = Flask(__name__)
FRONTEND_DIST = Path(__file__).resolve().parent
MAP_FILE = FRONTEND_DIST / "irigo_map.html"
REFRESH_INTERVAL_SECONDS = 10
PARIS_TIMEZONE = transit.LOCAL_TIMEZONE

_static_data = None
_static_data_lock = Lock()
_realtime_index = None
_realtime_index_updated_at = 0.0
_realtime_lock = Lock()


def get_static_data():
    global _static_data
    if _static_data is None:
        with _static_data_lock:
            if _static_data is None:
                stops, scheduled_departures, calendar, calendar_dates, trips = (
                    transit.load_gtfs_static()
                )
                _static_data = {
                    "stops": stops,
                    "scheduled_departures": scheduled_departures,
                    "calendar": calendar,
                    "calendar_dates": calendar_dates,
                    "trips": trips,
                    "served_stops": transit.find_served_stops_by_route(
                        stops,
                        scheduled_departures,
                        trips,
                    ),
                }
    return _static_data


def get_realtime_index():
    global _realtime_index, _realtime_index_updated_at
    with _realtime_lock:
        if (
            _realtime_index is None
            or time.monotonic() - _realtime_index_updated_at >= REFRESH_INTERVAL_SECONDS
        ):
            feed = transit.fetch_gtfs_realtime_trip_updates()
            _realtime_index = transit.build_realtime_index(feed)
            _realtime_index_updated_at = time.monotonic()
    return _realtime_index


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/carte")
def map_page():
    if not MAP_FILE.is_file():
        response = Response(
            """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Cache-Control" content="no-store">
    <title>Chargement de la cartographie</title>
  </head>
  <body>
    <p id="status">La carte se prépare. Cette page va se mettre à jour automatiquement.</p>
    <script>
      async function checkMap() {
        try {
          const response = await fetch("/carte", {
            method: "HEAD",
            cache: "no-store"
          });
          if (response.ok) {
            window.location.reload();
            return;
          }
        } catch (error) {
          document.getElementById("status").textContent =
            "La carte est toujours en préparation. Nouvelle vérification en cours…";
        }
        window.setTimeout(checkMap, 5000);
      }
      window.setTimeout(checkMap, 5000);
    </script>
  </body>
</html>""",
            status=503,
            mimetype="text/html",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Retry-After"] = "5"
        return response
    response = Response(
        """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Cache-Control" content="no-store">
    <title>Cartographie Irigo</title>
    <style>
      html, body, iframe {
        width: 100%;
        height: 100%;
        margin: 0;
        border: 0;
        overflow: hidden;
      }
      iframe {
        position: fixed;
        inset: 0;
      }
      .map-hidden {
        visibility: hidden;
      }
    </style>
  </head>
  <body>
    <iframe id="map-a" title="Cartographie Irigo" src="/carte/map"></iframe>
    <iframe id="map-b" title="Actualisation de la cartographie" class="map-hidden"></iframe>
    <script>
      (function() {
        const refreshInterval = 15000;
        let activeMap = document.getElementById("map-a");
        let loadingMap = document.getElementById("map-b");
        let refreshPending = false;

        function refreshMap() {
          if (refreshPending) return;
          refreshPending = true;
          loadingMap.onload = function() {
            loadingMap.classList.remove("map-hidden");
            activeMap.classList.add("map-hidden");
            const previousMap = activeMap;
            activeMap = loadingMap;
            loadingMap = previousMap;
            refreshPending = false;
          };
          loadingMap.src = "/carte/map?refresh=" + Date.now();
        }

        window.setInterval(refreshMap, refreshInterval);
      })();
    </script>
  </body>
</html>""",
        mimetype="text/html",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/carte/map")
def map_document():
    response = send_from_directory(MAP_FILE.parent, MAP_FILE.name)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/stops")
def stops():
    try:
        data = get_static_data()
    except requests.RequestException as error:
        app.logger.exception("Unable to download static GTFS data")
        return jsonify({"error": f"Impossible de charger les horaires GTFS : {error}"}), 502

    stop_options = [
        {
            "id": group_id,
            "name": stop_data["name"],
            "routeNames": sorted(stop_data["route_names"]),
        }
        for group_id, stop_data in data["served_stops"].items()
    ]
    stop_options.sort(key=lambda option: (transit.normalize_text(option["name"]), option["id"]))

    preferred_stop_ids = transit.find_stop_ids(data["stops"], ["proudhon"])
    preferred_stop_id = next(
        (
            transit.normalize_text(data["stops"].get(stop_id, stop_id)) or stop_id
            for stop_id in preferred_stop_ids
            if (transit.normalize_text(data["stops"].get(stop_id, stop_id)) or stop_id)
            in data["served_stops"]
        ),
        None,
    )
    return jsonify({"stops": stop_options, "preferredStopId": preferred_stop_id})


@app.get("/api/departures")
def departures():
    stop_group = transit.normalize_text(request.args.get("stop", ""))
    if not stop_group:
        return jsonify({"error": "Le paramètre d'arrêt « stop » est obligatoire."}), 400

    try:
        data = get_static_data()
        if stop_group not in data["served_stops"]:
            return jsonify({"error": "Arrêt inconnu."}), 404

        realtime_index = get_realtime_index()
        active_service_ids = transit.get_active_service_ids(
            data["calendar"],
            data["calendar_dates"],
        )
        origin_ids = [
            stop_id
            for stop_id, stop_name in data["stops"].items()
            if (transit.normalize_text(stop_name) or stop_id) == stop_group
        ]
        results = transit.select_trips(
            data["stops"],
            data["scheduled_departures"],
            realtime_index,
            origin_ids,
            active_service_ids,
            data["trips"],
            max_results=12,
        )
    except requests.RequestException as error:
        app.logger.exception("Unable to download GTFS data")
        return jsonify({"error": f"Impossible de récupérer les données temps réel : {error}"}), 502

    paris_time = datetime.now(PARIS_TIMEZONE).isoformat(timespec="seconds")
    return jsonify(
        {
            "updatedAt": paris_time,
            "departures": [
                {
                    "tripId": item["trip_id"],
                    "originStopName": item["origin_stop_name"],
                    "routeShortName": item["route_short_name"],
                    "routeColor": item["route_color"],
                    "destinationName": item["destination_name"],
                    "directionId": str(item.get("direction_id", "")),
                    "departureTime": datetime.fromtimestamp(
                        item["origin_time"],
                        PARIS_TIMEZONE,
                    ).strftime("%H:%M"),
                    "scheduledTime": datetime.fromtimestamp(
                        item["origin_schedule_time"],
                        PARIS_TIMEZONE,
                    ).strftime("%H:%M"),
                    "source": item["origin_source"],
                }
                for item in results
            ],
        }
    )


@app.get("/")
def index():
    if not (FRONTEND_DIST / "index.html").is_file():
        return jsonify({"error": "L'interface React n'a pas encore été compilée."}), 503
    return send_from_directory(FRONTEND_DIST, "index.html")


@app.get("/<path:asset_path>")
def frontend_asset(asset_path):
    asset = FRONTEND_DIST / asset_path
    if asset.is_file():
        return send_from_directory(FRONTEND_DIST, asset_path)
    if not (FRONTEND_DIST / "index.html").is_file():
        return jsonify({"error": "L'interface React n'a pas encore été compilée."}), 503
    return send_from_directory(FRONTEND_DIST, "index.html")
