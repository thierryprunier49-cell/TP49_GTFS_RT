#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""GTFS RT - Prochains passages depuis Proudhon vers le centre-ville 

Ce script télécharge les données GTFS statiques et le flux GTFS-RT (trip-updates),
calcule les prochains passages de bus depuis l'arrêt Proudhon vers deux destinations
et génère une page HTML résumant ces prochains passages.
"""

import csv
import html
import io
import time
import unicodedata
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

GTFS_STATIC_URL = "https://chouette.enroute.mobi/api/v1/datas/Irigo/gtfs.zip"
GTFS_RT_URL_TRIP_UPDATES = "https://ara-api.enroute.mobi/irigo/gtfs/trip-updates"
OUTPUT_HTML = Path(__file__).resolve().with_name("gtfs_rt_prouhon.html")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SEARCH_PAIRS = [
    (
        "Proudhon -> Centre Ville",
        ["proudhon"],
        ["foch-maison bleue"],
    ),
    
]


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
                    "route_short_name": route_info.get("route_short_name") or route_id,
                    "route_color": route_info.get("route_color") or "",
                }

    # Lecture des horaires par stop_times (stop_times.txt)
    # On ne conserve que les trips de bus (route_type == 3)
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
            if not route_id or not route_info or route_info.get("route_type") != 3:
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
    print(f"{len(scheduled_departures)} trips de bus chargés")
    print(f"{len(calendar)} services chargés")
    return stops, scheduled_departures, calendar, calendar_dates, trips


def get_active_service_ids(calendar, calendar_dates, target_date=None):
    """Détermine les service_ids actifs pour une date donnée.

    `target_date` doit être au format 'YYYYMMDD' (ex: '20260612').
    Si aucune date n'est fournie, utilise la date d'aujourd'hui.

    Retourne un ensemble de service_ids actifs.
    """
    if target_date is None:
        target_date = datetime.now().strftime("%Y%m%d")
    
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


def format_time(timestamp):
    """Formate un timestamp POSIX en 'HH:MM'.

    Ajoute 0 minute aux horaires affichés.
    """
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp + 0).strftime("%H:%M")


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


def build_html_report(reports, stops):
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
        '  <title>GTFS RT prochains passages vers le centre-ville</title>',
        '  <style>',
        '    body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f7f7f7; }',
        '    h1 { color: #222; }',
        '    .stop-picker { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; max-width: 980px; margin: 20px auto; }',
        '    .stop-picker select { max-width: 100%; padding: 10px; font: inherit; }',
        '    table { width: 100%; max-width: 980px; border-collapse: collapse; margin: 20px auto; background: white; font-size: 1.04rem; }',
        '    th, td { padding: 16px 12px; border: 1px solid #ddd; text-align: center; }',
        '    th:nth-child(1), td:nth-child(1) { width: 16%; }',
        '    th:nth-child(2), td:nth-child(2) { width: 24%; }',
        '    th:nth-child(3), td:nth-child(3) { width: 40%; }',
        '    th:nth-child(4), td:nth-child(4) { width: 20%; }',
        '    th { background: #FF9500; color: white; font-weight: bold; }',
        '    td { color: #333; }',
        '    .small { font-size: 0.95rem; color: #666; margin-top: 10px; }',
        '  </style>',
        '</head>',
        '<body>',
        '  <h1>Prochains passages vers le centre-ville</h1>',
        '  <div class="stop-picker">',
        '    <label for="origin-stop">Choisir un arrêt de départ :</label>',
        '    <select id="origin-stop" aria-label="Arrêt de départ">',
    ]

    origin_ids = sorted(
        {
            stop_id
            for report in reports
            for stop_id in report.get("origin_ids", [])
        },
        key=lambda stop_id: (normalize_text(stops.get(stop_id, stop_id)), stop_id),
    )
    origin_names = [stops.get(stop_id, stop_id) for stop_id in origin_ids]
    duplicate_names = {
        name for name in set(origin_names) if origin_names.count(name) > 1
    }
    preferred_origin_ids = {
        stop_id
        for report in reports
        for stop_id in report.get("preferred_origin_ids", [])
    }
    default_origin_id = next(
        (stop_id for stop_id in origin_ids if stop_id in preferred_origin_ids),
        None,
    )
    for stop_id in origin_ids:
        stop_name = stops.get(stop_id, stop_id)
        option_label = f"{stop_name} ({stop_id})" if stop_name in duplicate_names else stop_name
        selected = ' selected' if stop_id == default_origin_id else ''
        lines.append(
            f'      <option value="{html.escape(stop_id, quote=True)}"{selected}>'
            f'{html.escape(option_label)}</option>'
        )

    lines.extend([
        '    </select>',
        '  </div>',
        f'  <p class="small">Généré le {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>',
        '  <p class="small">*: horaire statique (planifié)</p>',
    ])

    all_rows = []
    for report in reports:
        # Choisir jusqu'à 6 prochains passages par sens, en privilégiant GTFS-RT
        results = report.get("results") or []
        if not results:
            continue

        rt_candidates = [r for r in results if r.get("origin_source") == "GTFS-RT" or r.get("destination_source") == "GTFS-RT"]
        if rt_candidates:
            ordered = sorted(rt_candidates, key=lambda x: x.get("destination_time") or float("inf"))
            remaining = [r for r in results if r not in rt_candidates]
            ordered.extend(sorted(remaining, key=lambda x: x.get("destination_time") or float("inf")))
        else:
            ordered = sorted(results, key=lambda x: x.get("destination_time") or float("inf"))

        selected = []
        seen_arrival_times = set()
        for candidate in ordered:
            arrival_time = format_time(candidate["origin_time"])
            if arrival_time in seen_arrival_times:
                continue
            selected.append(candidate)
            seen_arrival_times.add(arrival_time)
            if len(selected) == 6:
                break

        for next_item in selected:
            arrival_text = format_time(next_item["origin_time"])
            if next_item.get("origin_source") != "GTFS-RT":
                arrival_text += " *"

            all_rows.append(
                {
                    'line': format_route_badge(next_item.get("route_short_name", ""), next_item.get("route_color", "")),
                    'origin_id': html.escape(next_item["origin_stop_id"], quote=True),
                    'origin_name': html.escape(next_item["origin_stop_name"]),
                    'destination': 'Centre Ville',
                    'arrival': html.escape(arrival_text),
                    'scheduled': html.escape(format_time(next_item.get("origin_schedule_time", next_item.get("origin_time"))))
                }
            )

    lines.append('    <table>')
    lines.append('      <thead>')
    lines.append('        <tr><th>Ligne</th><th>Sens</th><th>Destination</th><th>Arrivée</th></tr>')
    lines.append('      </thead>')
    lines.append('      <tbody>')
    lines.append('      <tr id="no-passages" hidden><td colspan="4">Aucun prochain passage pour cet arrêt.</td></tr>')
    for row in all_rows:
        lines.append(
            f'      <tr data-origin-id="{row["origin_id"]}">'
            f'<td>{row["line"]}</td>'
            f'<td>{row["origin_name"]} -&gt; Centre Ville</td>'
            f'<td>{row["destination"]}</td>'
            f'<td>{row["arrival"]}</td>'
            '</tr>'
        )
    lines.append('      </tbody>')
    lines.append('    </table>')

    lines.extend([
        '  <script>',
        '    const stopPicker = document.getElementById("origin-stop");',
        '    const passageRows = document.querySelectorAll("tbody tr[data-origin-id]");',
        '    const noPassages = document.getElementById("no-passages");',
        '    const savedStopId = localStorage.getItem("gtfs-origin-stop");',
        '    if (savedStopId && [...stopPicker.options].some(option => option.value === savedStopId)) {',
        '      stopPicker.value = savedStopId;',
        '    }',
        '    function filterPassages() {',
        '      const selectedStopId = stopPicker.value;',
        '      let visibleRows = 0;',
        '      passageRows.forEach(row => {',
        '        row.hidden = row.dataset.originId !== selectedStopId;',
        '        if (!row.hidden) visibleRows += 1;',
        '      });',
        '      noPassages.hidden = visibleRows > 0;',
        '      localStorage.setItem("gtfs-origin-stop", selectedStopId);',
        '    }',
        '    stopPicker.addEventListener("change", filterPassages);',
        '    filterPassages();',
        '  </script>',
    ])
    lines.append('</body>')
    lines.append('</html>')
    return '\n'.join(lines)


def save_html_report(reports, stops, path):
    """Sauvegarde la page HTML générée vers `path`."""
    html_text = build_html_report(reports, stops)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html_text)
    print(f"Page HTML générée : {path}")


def select_trips(stops, scheduled_departures, realtime_index, origin_ids, dest_ids, active_service_ids, trips, max_results=5):
    """Sélectionne les prochains trajets entre `origin_ids` et `dest_ids`.

    Prend en compte les informations GTFS-RT (timestamp/delay) si présentes,
    sinon utilise l'horaire statique calculé à partir de `stop_times`.
    Filtre sur les trips dont le service_id est actif aujourd'hui.
    Retourne au plus `max_results` candidats triés par heure d'arrivée.
    """
    now = datetime.now()
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
        dest_entries = [entry for entry in stops_list if entry["stop_id"] in dest_ids]
        if not origin_entries or not dest_entries:
            continue

        for origin in origin_entries:
            for dest in dest_entries:
                if origin["stop_sequence"] >= dest["stop_sequence"]:
                    continue
                origin_schedule_ts = today_midnight + origin["departure_seconds"]
                dest_schedule_ts = today_midnight + dest["departure_seconds"]

                origin_rt = realtime_index.get((trip_id, origin["stop_id"]))
                dest_rt = realtime_index.get((trip_id, dest["stop_id"]))

                origin_ts = None
                dest_ts = None

                if origin_rt and origin_rt.get("timestamp"):
                    origin_ts = origin_rt["timestamp"]
                elif origin_rt and origin_rt.get("delay") is not None:
                    origin_ts = origin_schedule_ts + origin_rt["delay"]
                else:
                    origin_ts = origin_schedule_ts

                if dest_rt and dest_rt.get("timestamp"):
                    dest_ts = dest_rt["timestamp"]
                elif dest_rt and dest_rt.get("delay") is not None:
                    dest_ts = dest_schedule_ts + dest_rt["delay"]
                else:
                    dest_ts = dest_schedule_ts

                # Ignorer les départs passés (avec une petite tolérance)
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
                        "destination_stop_id": dest["stop_id"],
                        "destination_stop_name": stops.get(dest["stop_id"], dest["stop_id"]),
                        "destination_time": dest_ts,
                        "destination_schedule_time": dest_schedule_ts,
                        "destination_source": "GTFS-RT" if dest_rt else "statique",
                        "route_short_name": trip_service_info.get("route_short_name", ""),
                        "route_color": trip_service_info.get("route_color", ""),
                    }
                )

    candidates.sort(key=lambda item: (item["destination_time"], item["trip_id"]))
    if max_results is None:
        return candidates

    results_by_origin = {}
    for candidate in candidates:
        origin_id = candidate["origin_stop_id"]
        origin_results = results_by_origin.setdefault(origin_id, [])
        if len(origin_results) < max_results:
            origin_results.append(candidate)
    return [
        candidate
        for origin_results in results_by_origin.values()
        for candidate in origin_results
    ]


def find_origin_stop_ids(scheduled_departures, dest_ids):
    """Trouve les arrêts présents avant une destination dans les horaires."""
    origin_ids = set()
    for stops_list in scheduled_departures.values():
        destination_sequences = [
            entry["stop_sequence"]
            for entry in stops_list
            if entry["stop_id"] in dest_ids
        ]
        if not destination_sequences:
            continue
        first_destination_sequence = min(destination_sequences)
        origin_ids.update(
            entry["stop_id"]
            for entry in stops_list
            if entry["stop_sequence"] < first_destination_sequence
        )
    return sorted(origin_ids)


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
            f"Trip {item['trip_id']}: {item['origin_stop_name']} -> {item['destination_stop_name']} | "
            f"arrivée à l'origine {format_time(item['origin_time'])} ({item['origin_source']})"
        )


def main():
    """Point d'entrée principal: charge les données, calcule et génère le rapport."""
    # Charger les données statiques une seule fois
    stops, scheduled_departures, calendar, calendar_dates, trips = load_gtfs_static()
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
            for title, origin_patterns, dest_patterns in SEARCH_PAIRS:
                dest_ids = find_stop_ids(stops, dest_patterns)
                origin_ids = find_origin_stop_ids(scheduled_departures, dest_ids)
                preferred_origin_ids = find_stop_ids(stops, origin_patterns)

                print(f"\nRequête : {title}")
                print_candidates("origines", stops, origin_ids)
                print_candidates("destinations", stops, dest_ids)

                results = select_trips(stops, scheduled_departures, realtime_index, origin_ids, dest_ids, active_service_ids, trips, max_results=8)
                print_trip_results(title, results)

                reports.append(
                    {
                        "title": title,
                        "origin_ids": origin_ids,
                        "preferred_origin_ids": preferred_origin_ids,
                        "dest_ids": dest_ids,
                        "results": results,
                    }
                )

            save_html_report(reports, stops, OUTPUT_HTML)
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
