#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""GTFS RT - Prochains passages par arrêt

Ce script télécharge les données GTFS statiques et le flux GTFS-RT (trip-updates),
calcule les 12 prochains départs depuis un arrêt sélectionné, dans les deux sens,
et génère une page HTML résumant ces prochains passages.
"""

import csv
import html
import io
import json
import time
import unicodedata
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

GTFS_STATIC_URL = "https://chouette.enroute.mobi/api/v1/datas/Irigo/gtfs.zip"
GTFS_RT_URL_TRIP_UPDATES = "https://ara-api.enroute.mobi/irigo/gtfs/trip-updates"
OUTPUT_HTML = Path(__file__).resolve().with_name("gtfs_rt_prouhon.html")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SEARCH_PAIRS = [
    (
        "Prochains passages",
        ["proudhon"],
    ),
    
]
LOCAL_TIMEZONE = ZoneInfo("Europe/Paris")


def normalize_text(value):
    """Normalize une chaîne pour la recherche.

    - Retourne une chaîne vide si `value` est falsy.
    - Supprime les accents, met en minuscules et enlève les espaces en tête/fin.
    """
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return without_accents.lower().strip()


def load_gtfs_static():
    """Charge le GTFS statique depuis l'URL configurée.

    Retourne les structures:
    - stops: dict stop_id -> stop_name
    - scheduled_departures: dict trip_id -> liste d'objets stop_time planifiés
    - calendar: dict service_id -> infos de service (jours de la semaine)
    - calendar_dates: dict service_id -> liste des dates d'exception
    """
    print("Chargement du GTFS statique...")
    response = requests.get(GTFS_STATIC_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    archive = zipfile.ZipFile(io.BytesIO(response.content))

    stops = {}
    routes = {}
    trips = {}
    scheduled_departures = {}
    calendar = {}
    calendar_dates = {}

    # Lecture des arrêts (stops.txt)
    with archive.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        for row in reader:
            stop_id = row.get("stop_id")
            stop_name = row.get("stop_name", "").strip()
            if stop_id:
                stops[stop_id] = stop_name

    # Lecture des routes (routes.txt)
    with archive.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        for row in reader:
            route_id = row.get("route_id")
            try:
                route_type = int(row.get("route_type", "3") or "3")
            except ValueError:
                route_type = 3
            if route_id:
                routes[route_id] = {
                    "route_type": route_type,
                    "route_short_name": row.get("route_short_name", "").strip(),
                    "route_long_name": row.get("route_long_name", "").strip(),
                    "route_color": row.get("route_color", "").strip(),
                }

    # Lecture des trips (trips.txt)
    with archive.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        for row in reader:
            trip_id = row.get("trip_id")
            route_id = row.get("route_id")
            service_id = row.get("service_id")
            if trip_id and route_id:
                route_info = routes.get(route_id, {})
                trips[trip_id] = {
                    "route_id": route_id,
                    "service_id": service_id,
                    "trip_headsign": row.get("trip_headsign", "").strip(),
                    "direction_id": row.get("direction_id", "").strip(),
                    "route_short_name": route_info.get("route_short_name") or route_id,
                    "route_color": route_info.get("route_color") or "",
                }

    # Lecture des horaires par stop_times (stop_times.txt)
    # GTFS route_type 0 correspond aux tramways et 3 aux bus.
    with archive.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        for row in reader:
            trip_id = row.get("trip_id")
            stop_id = row.get("stop_id")
            if not trip_id or not stop_id:
                continue
            trip_info = trips.get(trip_id)
            if not trip_info:
                continue
            route_id = trip_info.get("route_id")
            route_info = routes.get(route_id)
            if not route_id or not route_info or route_info.get("route_type") not in (0, 3):
                continue
            stop_sequence = int(row.get("stop_sequence", "0") or "0")
            departure_time = row.get("departure_time") or row.get("arrival_time")
            if not departure_time:
                continue
            try:
                hours, minutes, seconds = departure_time.split(":")
                departure_seconds = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            except ValueError:
                continue
            scheduled_departures.setdefault(trip_id, []).append(
                {
                    "stop_id": stop_id,
                    "stop_sequence": stop_sequence,
                    "departure_time": departure_time,
                    "departure_seconds": departure_seconds,
                }
            )

    # Trier les arrêts pour chaque trip par séquence puis heure
    for stops_list in scheduled_departures.values():
        stops_list.sort(key=lambda item: (item["stop_sequence"], item["departure_seconds"]))

    # Lecture du calendrier (calendar.txt)
    try:
        with archive.open("calendar.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                service_id = row.get("service_id")
                if service_id:
                    calendar[service_id] = {
                        "monday": int(row.get("monday", "0")),
                        "tuesday": int(row.get("tuesday", "0")),
                        "wednesday": int(row.get("wednesday", "0")),
                        "thursday": int(row.get("thursday", "0")),
                        "friday": int(row.get("friday", "0")),
                        "saturday": int(row.get("saturday", "0")),
                        "sunday": int(row.get("sunday", "0")),
                        "start_date": row.get("start_date"),
                        "end_date": row.get("end_date"),
                    }
    except KeyError:
        print("Fichier calendar.txt non trouvé")

    # Lecture des exceptions de calendrier (calendar_dates.txt)
    try:
        with archive.open("calendar_dates.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            for row in reader:
                service_id = row.get("service_id")
                date = row.get("date")
                exception_type = int(row.get("exception_type", "0"))
                if service_id and date:
                    if service_id not in calendar_dates:
                        calendar_dates[service_id] = {}
                    calendar_dates[service_id][date] = exception_type
    except KeyError:
        print("Fichier calendar_dates.txt non trouvé")

    print(f"{len(stops)} arrêts chargés")
    print(f"{len(scheduled_departures)} trips de bus et tram chargés")
    print(f"{len(calendar)} services chargés")
    return stops, scheduled_departures, calendar, calendar_dates, trips


def get_active_service_ids(calendar, calendar_dates, target_date=None):
    """Détermine les service_ids actifs pour une date donnée.

    `target_date` doit être au format 'YYYYMMDD' (ex: '20260612').
    Si aucune date n'est fournie, utilise la date d'aujourd'hui.

    Retourne un ensemble de service_ids actifs.
    """
    if target_date is None:
        target_date = datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%d")
    
    active_services = set()
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    target_weekday = day_names[datetime.strptime(target_date, "%Y%m%d").weekday()]

    for service_id, info in calendar.items():
        # Vérifier que la date est dans la plage de validité
        start_date = info.get("start_date")
        end_date = info.get("end_date")
        if start_date and target_date < start_date:
            continue
        if end_date and target_date > end_date:
            continue

        # Vérifier si le jour de la semaine est actif
        if info.get(target_weekday, 0) == 1:
            # Vérifier les exceptions (calendar_dates)
            exceptions = calendar_dates.get(service_id, {})
            exception_type = exceptions.get(target_date)

            if exception_type == 2:  # Service supprimé pour cette date
                continue
            elif exception_type == 1:  # Service ajouté pour cette date
                active_services.add(service_id)
            elif exception_type is None:  # Pas d'exception, jour normal
                active_services.add(service_id)
        elif target_date in calendar_dates.get(service_id, {}) and calendar_dates[service_id][target_date] == 1:
            # Service ajouté même si le jour n'est pas actif normalement
            active_services.add(service_id)

    return active_services


def fetch_gtfs_realtime_trip_updates():
    """Récupère le flux GTFS-RT (trip-updates) et le parse en FeedMessage.

    Retourne un objet `gtfs_realtime_pb2.FeedMessage`.
    """
    print("Chargement du flux GTFS-RT trip-updates...")
    response = requests.get(GTFS_RT_URL_TRIP_UPDATES, headers=HEADERS, timeout=15)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    print(f"{len(feed.entity)} entités trip-update reçues")
    return feed


def build_realtime_index(trip_updates_feed):
    """Construit un index rapide: (trip_id, stop_id) -> {timestamp, delay}.

    L'index permet de retrouver rapidement les informations GTFS-RT
    associées à un couple (trip_id, stop_id).
    """
    realtime = {}
    for entity in trip_updates_feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip_id = trip_update.trip.trip_id if trip_update.trip.trip_id else None
        if not trip_id:
            continue
        for stu in trip_update.stop_time_update:
            stop_id = stu.stop_id if stu.stop_id else None
            if not stop_id:
                continue
            timestamp = None
            delay = None
            if stu.HasField("departure"):
                if stu.departure.HasField("time"):
                    timestamp = stu.departure.time
                if stu.departure.HasField("delay"):
                    delay = stu.departure.delay
            # Si pas de time de départ, essayer l'heure d'arrivée
            if timestamp is None and stu.HasField("arrival"):
                if stu.arrival.HasField("time"):
                    timestamp = stu.arrival.time
                if stu.arrival.HasField("delay"):
                    delay = stu.arrival.delay
            if timestamp is None and delay is None:
                continue
            realtime[(trip_id, stop_id)] = {
                "timestamp": int(timestamp) if timestamp is not None else None,
                "delay": int(delay) if delay is not None else None,
            }
    return realtime


def find_stop_ids(stops, patterns):
    """Trouve les stop_ids dont le nom contient l'une des `patterns`.

    La recherche est normalisée (sans accents, insensible à la casse).
    Retourne une liste triée de stop_ids.
    """
    normalized_patterns = [normalize_text(p) for p in patterns]
    result = [
        stop_id
        for stop_id, name in stops.items()
        if any(p in normalize_text(name) for p in normalized_patterns)
    ]
    return sorted(result)


def find_served_stops_by_route(stops, scheduled_departures, trips):
    """Construit l'association entre chaque arrêt et les lignes qui le desservent."""
    served_stops = {}
    for trip_id, stop_entries in scheduled_departures.items():
        trip_info = trips.get(trip_id)
        if not trip_info:
            continue
        route_name = str(trip_info.get("route_short_name", ""))
        if not route_name:
            continue

        for entry in stop_entries:
            stop_id = entry["stop_id"]
            stop_name = stops.get(stop_id, stop_id)
            stop_group = normalize_text(stop_name) or stop_id
            stop_data = served_stops.setdefault(
                stop_group,
                {"name": stop_name, "route_names": set()},
            )
            stop_data["route_names"].add(route_name)

    return served_stops


def format_time(timestamp):
    """Formate un timestamp POSIX en 'HH:MM'.

    Ajoute 0 minute aux horaires affichés.
    """
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, LOCAL_TIMEZONE).strftime("%H:%M")


def format_route_badge(route_short_name, route_color):
    """Retourne un badge HTML pour la ligne avec la couleur de la route."""
    color = (route_color or "").strip()
    if color and not color.startswith("#"):
        color = f"#{color}"
    if not color or len(color) != 7:
        color = "#444"

    try:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        text_color = "#000" if brightness > 180 else "#fff"
    except ValueError:
        color = "#444"
        text_color = "#fff"

    return (
        f'<span style="display:inline-block; min-width:42px; padding:4px 8px; '
        f'border-radius:10px; background:{color}; color:{text_color}; font-weight:700;">'
        f'{html.escape(route_short_name)}</span>'
    )


def build_html_report(reports, stops, served_stops_by_route):
    """Génère le contenu HTML pour la liste de `reports`.

    `reports` est une liste d'objets contenant les résultats pour chaque paire.
    Retourne le texte HTML complet.
    """
    lines = [
        '<!doctype html>',
        '<html lang="fr">',
        '<head>',
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        '  <meta http-equiv="refresh" content="10">',
        '  <title>GTFS RT - prochains passages</title>',
        '  <style>',
        '    body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f7f7f7; }',
        '    h1 { color: #222; }',
        '    .stop-picker { display: flex; flex-wrap: nowrap; align-items: center; gap: 10px; width: 100%; max-width: 1400px; margin: 20px auto; overflow-x: auto; }',
        '    .stop-picker select { max-width: 100%; padding: 10px; font: inherit; }',
        '    table { width: 100%; max-width: 1400px; border-collapse: collapse; margin: 20px auto; background: white; font-size: 1.04rem; }',
        '    th, td { padding: 16px 12px; border: 1px solid #ddd; text-align: center; }',
        '    th:nth-child(1), td:nth-child(1) { width: 16%; }',
        '    th:nth-child(2), td:nth-child(2) { width: 24%; }',
        '    th:nth-child(3), td:nth-child(3) { width: 40%; }',
        '    th:nth-child(4), td:nth-child(4) { width: 20%; }',
        '    th { background: #FF9500; color: white; font-weight: bold; }',
        '    td { color: #333; }',
        '    .small { font-size: 0.95rem; color: #666; margin: 0; }',
        '    .page-footer { position: fixed; right: 20px; bottom: 12px; text-align: right; }',
        '  </style>',
        '</head>',
        '<body>',
        '  <h1>Prochains passages</h1>',
        '  <div class="stop-picker">',
        '    <label for="origin-stop">Choisir un arrêt de départ :</label>',
        '    <select id="origin-stop" aria-label="Arrêt de départ">',
    ]

    preferred_origin_ids = {
        stop_id
        for report in reports
        for stop_id in report.get("preferred_origin_ids", [])
    }
    preferred_origin_groups = {
        normalize_text(stops.get(stop_id, stop_id)) or stop_id
        for stop_id in preferred_origin_ids
    }
    sorted_stop_groups = sorted(
        served_stops_by_route,
        key=lambda group_id: (
            normalize_text(served_stops_by_route[group_id]["name"]),
            group_id,
        ),
    )
    default_origin_id = next(
        (
            group_id
            for group_id in sorted_stop_groups
            if group_id in preferred_origin_groups
        ),
        None,
    )
    for group_id in sorted_stop_groups:
        stop_data = served_stops_by_route[group_id]
        selected = ' selected' if group_id == default_origin_id else ''
        route_names = html.escape(
            json.dumps(sorted(stop_data["route_names"]), ensure_ascii=False),
            quote=True,
        )
        lines.append(
            f'      <option value="{html.escape(group_id, quote=True)}" '
            f'data-route-names="{route_names}"{selected}>'
            f'{html.escape(stop_data["name"])}</option>'
        )

    route_names = sorted(
        {
            route_name
            for stop_data in served_stops_by_route.values()
            for route_name in stop_data["route_names"]
        },
        key=lambda route_name: (normalize_text(route_name), route_name),
    )
    lines.extend([
        '    </select>',
        '    <label for="route-filter">Filtrer par ligne :</label>',
        '    <select id="route-filter" aria-label="Filtrer par ligne">',
        '      <option value="">Toutes les lignes</option>',
    ])
    for route_name in route_names:
        lines.append(
            f'      <option value="{html.escape(route_name, quote=True)}">'
            f'{html.escape(route_name)}</option>'
        )

    lines.extend([
        '    </select>',
        '    <label for="direction-filter">Direction :</label>',
        '    <select id="direction-filter" aria-label="Direction">',
        '      <option value="">Toutes les directions</option>',
        '      <option value="1">Centre-ville</option>',
        '      <option value="0">Extérieur</option>',
        '    </select>',
        '  </div>',
        '  <footer class="page-footer">',
        f'    <p class="small">Généré le {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>',
        '    <p class="small">*: horaire statique (planifié)</p>',
        '  </footer>',
    ])

    all_rows = []
    for report in reports:
        for next_item in report.get("results") or []:
            arrival_text = format_time(next_item["origin_time"])
            if next_item.get("origin_source") != "GTFS-RT":
                arrival_text += " *"

            all_rows.append(
                {
                    'line': format_route_badge(next_item.get("route_short_name", ""), next_item.get("route_color", "")),
                    'origin_group': html.escape(
                        normalize_text(next_item["origin_stop_name"]) or next_item["origin_stop_id"],
                        quote=True,
                    ),
                    'origin_name_attr': html.escape(next_item["origin_stop_name"], quote=True),
                    'route_name': html.escape(
                        str(next_item.get("route_short_name", "")),
                        quote=True,
                    ),
                    'direction_id': html.escape(
                        str(next_item.get("direction_id", "")),
                        quote=True,
                    ),
                    'origin_name': html.escape(next_item["origin_stop_name"]),
                    'destination': html.escape(next_item["destination_name"]),
                    'arrival': html.escape(arrival_text),
                    'scheduled': html.escape(format_time(next_item.get("origin_schedule_time", next_item.get("origin_time"))))
                }
            )

    lines.append('    <table>')
    lines.append('      <thead>')
    lines.append('        <tr><th>Ligne</th><th>Départ</th><th>Destination</th><th>Heure</th></tr>')
    lines.append('      </thead>')
    lines.append('      <tbody>')
    lines.append('      <tr id="no-passages" hidden><td colspan="4">Aucun prochain passage pour cet arrêt.</td></tr>')
    for row in all_rows:
        lines.append(
            f'      <tr data-origin-group="{row["origin_group"]}" '
            f'data-route-name="{row["route_name"]}" '
            f'data-direction-id="{row["direction_id"]}" '
            f'data-origin-name="{row["origin_name_attr"]}">'
            f'<td>{row["line"]}</td>'
            f'<td>{row["origin_name"]}</td>'
            f'<td>{row["destination"]}</td>'
            f'<td>{row["arrival"]}</td>'
            '</tr>'
        )
    lines.append('      </tbody>')
    lines.append('    </table>')

    lines.extend([
        '  <script>',
        '    const stopPicker = document.getElementById("origin-stop");',
        '    const routeFilter = document.getElementById("route-filter");',
        '    const directionFilter = document.getElementById("direction-filter");',
        '    const passageRows = document.querySelectorAll("tbody tr[data-origin-group]");',
        '    const noPassages = document.getElementById("no-passages");',
        '    const allStopOptions = [...stopPicker.options];',
        '    const savedStopId = localStorage.getItem("gtfs-origin-stop");',
        '    const savedRouteName = localStorage.getItem("gtfs-route-filter");',
        '    const savedDirection = localStorage.getItem("gtfs-direction-filter");',
        '    if (savedRouteName && [...routeFilter.options].some(option => option.value === savedRouteName)) {',
        '      routeFilter.value = savedRouteName;',
        '    }',
        '    if ([...directionFilter.options].some(option => option.value === savedDirection)) {',
        '      directionFilter.value = savedDirection;',
        '    }',
        '    const defaultStopId = stopPicker.value;',
        '    function updateStopOptions(preferredStopId) {',
        '      const availableStopOptions = allStopOptions.filter(option =>',
        '        !routeFilter.value || JSON.parse(option.dataset.routeNames).includes(routeFilter.value)',
        '      );',
        '      stopPicker.replaceChildren(...availableStopOptions);',
        '      const availableStopIds = new Set(availableStopOptions.map(option => option.value));',
        '      const nextStopId = [preferredStopId, defaultStopId].find(id => availableStopIds.has(id));',
        '      stopPicker.value = nextStopId || (availableStopOptions.length ? availableStopOptions[0].value : "");',
        '    }',
        '    function filterPassages() {',
        '      const selectedStopId = stopPicker.value;',
        '      const selectedRouteName = routeFilter.value;',
        '      const selectedDirection = directionFilter.value;',
        '      let visibleRows = 0;',
        '      passageRows.forEach(row => {',
        '        row.hidden = row.dataset.originGroup !== selectedStopId ||',
        '          (selectedRouteName && row.dataset.routeName !== selectedRouteName) ||',
        '          (selectedDirection && row.dataset.directionId !== selectedDirection);',
        '        if (!row.hidden) visibleRows += 1;',
        '      });',
        '      noPassages.hidden = visibleRows > 0;',
        '      localStorage.setItem("gtfs-origin-stop", selectedStopId);',
        '      localStorage.setItem("gtfs-route-filter", selectedRouteName);',
        '      localStorage.setItem("gtfs-direction-filter", selectedDirection);',
        '    }',
        '    stopPicker.addEventListener("change", filterPassages);',
        '    routeFilter.addEventListener("change", () => {',
        '      updateStopOptions(stopPicker.value);',
        '      filterPassages();',
        '    });',
        '    directionFilter.addEventListener("change", filterPassages);',
        '    updateStopOptions(savedStopId || defaultStopId);',
        '    filterPassages();',
        '  </script>',
    ])
    lines.append('</body>')
    lines.append('</html>')
    return '\n'.join(lines)


def save_html_report(reports, stops, served_stops_by_route, path):
    """Sauvegarde la page HTML générée vers `path`."""
    html_text = build_html_report(reports, stops, served_stops_by_route)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html_text)
    print(f"Page HTML générée : {path}")


def select_trips(stops, scheduled_departures, realtime_index, origin_ids, active_service_ids, trips, max_results=12):
    """Retourne les prochains départs par arrêt, toutes directions confondues.

    Prend en compte les informations GTFS-RT (timestamp/delay) si présentes,
    sinon utilise l'horaire statique calculé à partir de `stop_times`.
    Filtre sur les trips dont le service_id est actif aujourd'hui.
    Retourne jusqu'à `max_results` candidats par arrêt d'origine.
    """
    now = datetime.now(LOCAL_TIMEZONE)
    now_ts = int(now.timestamp())
    today_midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    candidates = []

    for trip_id, stops_list in scheduled_departures.items():
        # Vérifier que le trip est actif aujourd'hui
        trip_service_info = trips.get(trip_id)
        if not trip_service_info:
            continue
        service_id = trip_service_info.get("service_id")
        if service_id and service_id not in active_service_ids:
            continue

        origin_entries = [entry for entry in stops_list if entry["stop_id"] in origin_ids]
        if not origin_entries:
            continue

        destination_name = trip_service_info.get("trip_headsign")
        if not destination_name:
            final_stop_id = stops_list[-1]["stop_id"]
            destination_name = stops.get(final_stop_id, final_stop_id)

        for origin in origin_entries:
            origin_schedule_ts = today_midnight + origin["departure_seconds"]
            origin_rt = realtime_index.get((trip_id, origin["stop_id"]))
            if origin_rt and origin_rt.get("timestamp"):
                origin_ts = origin_rt["timestamp"]
            elif origin_rt and origin_rt.get("delay") is not None:
                origin_ts = origin_schedule_ts + origin_rt["delay"]
            else:
                origin_ts = origin_schedule_ts

            if origin_ts < now_ts - 60:
                continue

            candidates.append(
                {
                    "trip_id": trip_id,
                    "origin_stop_id": origin["stop_id"],
                    "origin_stop_name": stops.get(origin["stop_id"], origin["stop_id"]),
                    "origin_time": origin_ts,
                    "origin_schedule_time": origin_schedule_ts,
                    "origin_source": "GTFS-RT" if origin_rt else "statique",
                    "destination_name": destination_name,
                    "direction_id": trip_service_info.get("direction_id", ""),
                    "route_short_name": trip_service_info.get("route_short_name", ""),
                    "route_color": trip_service_info.get("route_color", ""),
                }
            )

    candidates.sort(key=lambda item: (item["origin_time"], item["trip_id"]))
    if max_results is None:
        return candidates

    results_by_origin = {}
    for candidate in candidates:
        origin_group = normalize_text(candidate["origin_stop_name"]) or candidate["origin_stop_id"]
        origin_results = results_by_origin.setdefault(origin_group, [])
        if len(origin_results) < max_results:
            origin_results.append(candidate)
    return [
        candidate
        for origin_results in results_by_origin.values()
        for candidate in origin_results
    ]


def print_candidates(title, stops, candidate_ids):
    """Affiche en console les stop_ids candidats pour une recherche."""
    if not candidate_ids:
        print(f"  Aucun arrêt trouvé pour {title}")
        return
    print(f"  Arrêts candidats pour {title} ({len(candidate_ids)}):")
    for stop_id in candidate_ids[:20]:
        print(f"    {stop_id} — {stops.get(stop_id, 'N/A')}")
    if len(candidate_ids) > 20:
        print(f"    ...et {len(candidate_ids) - 20} autres arrêts")


def print_trip_results(title, results):
    """Affiche les résultats de trajet formatés en console."""
    print(f"\n== {title} ==")
    if not results:
        print("Aucun prochain trajet identifié pour cette paire.")
        return
    for item in results:
        print(
            f"Trip {item['trip_id']}: {item['origin_stop_name']} -> {item['destination_name']} | "
            f"départ {format_time(item['origin_time'])} ({item['origin_source']})"
        )


def main():
    """Point d'entrée principal: charge les données, calcule et génère le rapport."""
    # Charger les données statiques une seule fois
    stops, scheduled_departures, calendar, calendar_dates, trips = load_gtfs_static()
    served_stops_by_route = find_served_stops_by_route(
        stops,
        scheduled_departures,
        trips,
    )
    browser_opened = False
    
    # Boucle de rafraîchissement toutes les 10 secondes
    while True:
        try:
            print(f"\n--- Rafraîchissement à {datetime.now().strftime('%H:%M:%S')} ---")
            trail = fetch_gtfs_realtime_trip_updates()
            realtime_index = build_realtime_index(trail)
            
            # Déterminer les services actifs pour aujourd'hui
            active_service_ids = get_active_service_ids(calendar, calendar_dates)
            print(f"Services actifs aujourd'hui : {len(active_service_ids)}")

            reports = []
            preferred_origin_ids = sorted(
                {
                    stop_id
                    for _, origin_patterns in SEARCH_PAIRS
                    for stop_id in find_stop_ids(stops, origin_patterns)
                }
            )
            origin_ids = sorted(
                {
                    entry["stop_id"]
                    for stop_entries in scheduled_departures.values()
                    for entry in stop_entries
                }
            )
            print(f"\nArrêts desservis : {len(served_stops_by_route)}")
            print_candidates("arrêts préférés", stops, preferred_origin_ids)

            results = select_trips(
                stops,
                scheduled_departures,
                realtime_index,
                origin_ids,
                active_service_ids,
                trips,
                max_results=12,
            )
            print(
                f"{len(results)} prochains passages calculés "
                f"sur {len(origin_ids)} arrêts desservis."
            )

            reports.append(
                {
                    "title": "Tous les arrêts desservis",
                    "origin_ids": origin_ids,
                    "preferred_origin_ids": preferred_origin_ids,
                    "results": results,
                }
            )

            save_html_report(reports, stops, served_stops_by_route, OUTPUT_HTML)
            if not browser_opened:
                if not webbrowser.open(OUTPUT_HTML.as_uri()):
                    print(f"Impossible d'ouvrir le rapport dans le navigateur : {OUTPUT_HTML}")
                browser_opened = True
            
            # Attendre 10 secondes avant le prochain rafraîchissement
            print("En attente du prochain rafraîchissement dans 10 secondes...")
            time.sleep(10)
        except KeyboardInterrupt:
            print("\nArrêt du script.")
            break
        except Exception as e:
            print(f"Erreur : {e}")
            print("Nouvelle tentative dans 10 secondes...")
            time.sleep(10)


if __name__ == "__main__":
    main()
