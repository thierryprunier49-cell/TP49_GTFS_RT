#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import requests
import folium
import zipfile
import io
import csv
import time
from datetime import datetime
from google.transit import gtfs_realtime_pb2
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

GTFS_STATIC_URL = "https://chouette.enroute.mobi/api/v1/datas/Irigo/gtfs.zip"
GTFS_RT_URL = "https://ara-api.enroute.mobi/irigo/gtfs/vehicle-positions"
GTFS_RT_URL_TRIP_UPDATES = "https://ara-api.enroute.mobi/irigo/gtfs/trip-updates"
SIRI_URL = "https://ara-api.enroute.mobi/irigo/siri"
SIRI_USE = os.environ.get("SIRI_USE", "true").lower() in ("1", "true", "yes")
SIRI_REQUESTOR_REF = os.environ.get("SIRI_REQUESTOR_REF", "irigo_map")
SIRI_PREVIEW_INTERVAL = os.environ.get("SIRI_PREVIEW_INTERVAL", "PT1H")
SIRI_MAX_JOURNEY_LENGTH = os.environ.get("SIRI_MAX_JOURNEY_LENGTH", "PT2H")
SIRI_ACCESS_TOKEN = os.environ.get("SIRI_ACCESS_TOKEN")
SIRI_API_KEY = os.environ.get("SIRI_API_KEY")
SIRI_API_KEY_HEADER = os.environ.get("SIRI_API_KEY_HEADER", "X-API-Key")


headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

print("📥 Chargement initial des données GTFS statiques...")
routes_data = {}
shapes_data = defaultdict(list)
trips_data = {}
stops_data = {}
stops_coords = {}
scheduled_departures = defaultdict(list)


def build_siri_vehicle_monitoring_request():
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Siri xmlns=\"http://www.siri.org.uk/siri\" version=\"2.0\">
  <ServiceRequest>
    <RequestTimestamp>{now}</RequestTimestamp>
    <RequestorRef>{SIRI_REQUESTOR_REF}</RequestorRef>
    <VehicleMonitoringRequest>
      <RequestTimestamp>{now}</RequestTimestamp>
      <PreviewInterval>{SIRI_PREVIEW_INTERVAL}</PreviewInterval>
      <MaximumJourneyLength>{SIRI_MAX_JOURNEY_LENGTH}</MaximumJourneyLength>
    </VehicleMonitoringRequest>
  </ServiceRequest>
</Siri>"""


def _find_text(elem, tag_localname):
    if elem is None:
        return None
    for child in elem.iter():
        tag = child.tag
        if isinstance(tag, str) and (tag.endswith("}" + tag_localname) or tag == tag_localname):
            return child.text
    return None


def parse_siri_vehicle_monitoring(content):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(content)
    vehicles = []
    for activity in root.findall('.//{http://www.siri.org.uk/siri}VehicleActivity'):
        mvj = activity.find('.//{http://www.siri.org.uk/siri}MonitoredVehicleJourney')
        if mvj is None:
            continue

        vehicle_location = mvj.find('.//{http://www.siri.org.uk/siri}VehicleLocation')
        if vehicle_location is None:
            vehicle_location = mvj.find('.//VehicleLocation')

        lattice = _find_text(vehicle_location, 'Latitude')
        longitude = _find_text(vehicle_location, 'Longitude')
        bearing = _find_text(vehicle_location, 'Bearing')
        speed = _find_text(vehicle_location, 'Speed')

        vehicle_id = _find_text(mvj, 'VehicleRef') or _find_text(mvj, 'VehicleID') or 'N/A'
        route_id = _find_text(mvj, 'LineRef') or 'N/A'
        dest = _find_text(mvj, 'DestinationName') or ''
        trip_id = _find_text(mvj, 'DatedVehicleJourneyRef') or _find_text(mvj, 'FramedVehicleJourneyRef') or 'N/A'
        expected = _find_text(mvj, 'ExpectedDepartureTime') or _find_text(mvj, 'ExpectedArrivalTime')

        try:
            latitude = float(lattice) if lattice else None
        except Exception:
            latitude = None
        try:
            longitude = float(longitude) if longitude else None
        except Exception:
            longitude = None
        try:
            bearing = float(bearing) if bearing else None
        except Exception:
            bearing = None
        try:
            speed = float(speed) if speed else None
        except Exception:
            speed = None

        vehicles.append({
            'vehicle_id': vehicle_id,
            'route_id': route_id,
            'trip_id': trip_id,
            'dest': dest,
            'latitude': latitude,
            'longitude': longitude,
            'bearing': bearing,
            'speed': speed,
            'expected_time': expected,
        })
    return vehicles


def fetch_siri_vehicle_positions():
    if not SIRI_USE:
        return None
    try:
        body = build_siri_vehicle_monitoring_request()
        siri_headers = {**headers, 'Content-Type': 'application/xml'}
        if SIRI_ACCESS_TOKEN:
            siri_headers['Authorization'] = SIRI_ACCESS_TOKEN
        elif SIRI_API_KEY:
            siri_headers[SIRI_API_KEY_HEADER] = SIRI_API_KEY
        response = requests.post(SIRI_URL, headers=siri_headers, data=body.encode('utf-8'), timeout=15)
        if response.status_code != 200:
            raise Exception(f"{response.status_code} {response.text.strip()[:200]}")
        return parse_siri_vehicle_monitoring(response.content)
    except Exception as e:
        print(f"⚠️  Erreur SIRI : {e}")
        return None


def parse_gtfs_time(time_str):
    try:
        hours, minutes, seconds = time_str.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    except Exception:
        return None


def format_seconds_as_time(seconds):
    seconds = int(seconds) % 86400
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def get_current_seconds():
    now = datetime.now()
    return now.hour * 3600 + now.minute * 60 + now.second


def get_next_scheduled_departure(trip_id, reference_seconds=None):
    stops = scheduled_departures.get(trip_id)
    if not stops:
        return None
    if reference_seconds is None:
        reference_seconds = get_current_seconds()
    for stop in stops:
        if stop['departure_seconds'] >= reference_seconds:
            return stop
    return None


def get_terminus_departure(trip_id):
    stops = scheduled_departures.get(trip_id)
    if not stops:
        return None
    return stops[-1]

try:
    response = requests.get(GTFS_STATIC_URL, headers=headers, timeout=30)
    if response.status_code == 200:
        z = zipfile.ZipFile(io.BytesIO(response.content))
        
        try:
            with z.open("routes.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
                for row in reader:
                    route_id = row['route_id']
                    routes_data[route_id] = {
                        'name': row.get('route_short_name', row.get('route_long_name', route_id)),
                        'type': int(row.get('route_type', 3)),
                        'color': row.get('route_color', 'FF0000')
                    }
        except KeyError:
            print("⚠️  Fichier routes.txt non trouvé")
        
        try:
            with z.open("shapes.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
                for row in reader:
                    shape_id = row['shape_id']
                    lat = float(row['shape_pt_lat'])
                    lon = float(row['shape_pt_lon'])
                    shapes_data[shape_id].append([lat, lon])
        except KeyError:
            print("⚠️  Fichier shapes.txt non trouvé")
        
        try:
            with z.open("trips.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
                for row in reader:
                    trip_id = row['trip_id']
                    trips_data[trip_id] = {
                        'route_id': row['route_id'],
                        'shape_id': row.get('shape_id', None),
                        'headsign': row.get('trip_headsign', '')
                    }
        except KeyError:
            print("⚠️  Fichier trips.txt non trouvé")
        
        try:
            with z.open("stops.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
                for row in reader:
                    stop_id = row['stop_id']
                    stops_data[stop_id] = row.get('stop_name') or row.get('stop_desc') or stop_id
                    try:
                        lat = float(row.get('stop_lat'))
                        lon = float(row.get('stop_lon'))
                        stops_coords[stop_id] = (lat, lon)
                    except Exception:
                        pass
        except KeyError:
            print("⚠️  Fichier stops.txt non trouvé")
        
        try:
            with z.open("stop_times.txt") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
                for row in reader:
                    trip_id = row['trip_id']
                    departure_time = row.get('departure_time')
                    arrival_time = row.get('arrival_time')
                    stop_sequence = int(row.get('stop_sequence', '0') or 0)
                    if not departure_time and not arrival_time:
                        continue
                    departure_seconds = parse_gtfs_time(departure_time) if departure_time else None
                    arrival_seconds = parse_gtfs_time(arrival_time) if arrival_time else None
                    if departure_time and departure_seconds is None:
                        continue
                    if not departure_time and arrival_time and arrival_seconds is None:
                        continue
                    scheduled_departures[trip_id].append({
                        'departure_time': departure_time or arrival_time,
                        'departure_seconds': departure_seconds if departure_seconds is not None else arrival_seconds,
                        'arrival_time': arrival_time,
                        'arrival_seconds': arrival_seconds,
                        'stop_sequence': stop_sequence,
                        'stop_id': row.get('stop_id', '')
                    })
            for trip_id, stops in scheduled_departures.items():
                stops.sort(key=lambda x: ((x['departure_seconds'] if x['departure_seconds'] is not None else x['arrival_seconds']), x['stop_sequence']))
        except KeyError:
            print("⚠️  Fichier stop_times.txt non trouvé")
        
        print(f"✅ {len(routes_data)} lignes chargées")
        print(f"✅ {len(shapes_data)} trajets chargés")
        print(f"✅ {len(trips_data)} voyages chargés")
        print(f"✅ {len(stops_data)} arrêts chargés")
        print(f"✅ {len(scheduled_departures)} horaires de départ chargés")
except Exception as e:
    print(f"⚠️  Erreur GTFS: {e}")


ROUTE_TYPE_STYLES = {
    0: {"name": "Tram", "emoji": "🚊", "shape": "circle"},
    1: {"name": "Métro", "emoji": "🚇", "shape": "rounded"},
    2: {"name": "Train", "emoji": "🚆", "shape": "diamond"},
    3: {"name": "Bus", "emoji": "🚌", "shape": "square"},
    4: {"name": "Ferry", "emoji": "⛴️", "shape": "triangle"},
    5: {"name": "Tram", "emoji": "🚊", "shape": "circle"},
    7: {"name": "Téléphérique", "emoji": "🚠", "shape": "triangle"},
    11: {"name": "Funiculaire", "emoji": "🚡", "shape": "diamond"},
}


def route_id_number(route_id):
    try:
        return int(str(route_id))
    except (TypeError, ValueError):
        return None


def get_route_style(route_info, route_id=None):
    route_type = route_info.get('type', 3)
    route_color = route_info.get('color', 'FF0000') or 'FF0000'
    color_hex = '#' + route_color.lstrip('#')
    if len(color_hex) == 4:
        color_hex = '#' + ''.join(c*2 for c in color_hex.lstrip('#'))

    def get_text_color(hex_color):
        rgb = [int(hex_color[i:i+2], 16) for i in (1, 3, 5)]
        luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        return 'black' if luminance > 180 else 'white'

    type_style = ROUTE_TYPE_STYLES.get(route_type, {"name": "Autre", "emoji": "❓", "shape": "rounded"})
    route_number = route_id_number(route_id)
    route_name = str(route_info.get('name', '')).strip().upper()
    is_protected_tram = route_type in (0, 5) or route_name in {'A', 'B', 'C'}
    if route_number is not None and route_number > 20 and not is_protected_tram:
        type_style = {**type_style, 'shape': 'diamond'}

    return {
        'route_type_name': type_style['name'],
        'route_emoji': type_style['emoji'],
        'icon_shape': type_style['shape'],
        'bg_color': color_hex,
        'text_color': get_text_color(color_hex),
    }


def create_custom_icon(route_name, vehicle_id, style, destination="", departure_time=None, departure_source='N/A'):
    route_display = str(route_name)[:4]
    vehicle_display = str(vehicle_id)[-4:] if vehicle_id != "N/A" else ""
    dest = str(destination) if destination is not None else ""
    if dest:
        dest_display = dest if len(dest) <= 12 else dest[:11] + '…'
    else:
        dest_display = vehicle_display
    shape = style['icon_shape']
    bg_color = style['bg_color']
    text_color = style['text_color']

    source_border_colors = {
        'realtime': '#34A853',
        'realtime-siri': '#1E88E5',
        'static': '#F4B400',
        'N/A': '#888888'
    }
    border_color = source_border_colors.get(departure_source, '#888888')

    departure_html = ''
    if departure_time:
        departure_html = (
            f'<div style="font-size: 8px; line-height: 1; opacity: 0.9;">Départ {departure_time}</div>'
        )

    shape_styles = {
        'circle': 'border-radius: 50%;',
        'square': 'border-radius: 10%;',
        'rounded': 'border-radius: 14px;',
        'diamond': 'transform: rotate(45deg);',
        'triangle': 'clip-path: polygon(50% 0%, 0% 100%, 100% 100%);',
    }
    outer_style = (
        f"width: 45px; height: 45px; background-color: {bg_color}; "
        f"border: 3px solid {border_color}; {shape_styles.get(shape, '')} "
        "display: flex; align-items: center; justify-content: center; "
        "box-shadow: 0 2px 8px rgba(0,0,0,0.3); cursor: pointer;"
    )
    inner_transform = 'transform: rotate(-45deg);' if shape == 'diamond' else ''

    html = f"""
    <div style="position: relative; {outer_style}">
        <div style="{inner_transform} text-align: center; color: {text_color}; font-size: 12px; font-weight: bold;">
            <div style="line-height: 1;">{route_display}</div>
            <div style="font-size: 8px; line-height: 1;">{dest_display}</div>
            {departure_html}
        </div>
    </div>
    """
    return folium.DivIcon(html=html)


def fetch_gtfs_realtime_feed(url, label, cache_file=None):
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            raise Exception(f"{response.status_code}")
        feed.ParseFromString(response.content)
        print(f"✅ Données {label} reçues ({len(feed.entity)} entités)")
        return feed
    except Exception as e:
        print(f"⚠️  Erreur {label} : {e}")
        if cache_file:
            try:
                with open(cache_file, "rb") as f:
                    feed.ParseFromString(f.read())
                print(f"✅ Fichier local chargé pour {label}")
                return feed
            except FileNotFoundError:
                print(f"❌ Aucun fichier local trouvé pour {label}")
        return None


print("\n✅ Démarrage de la boucle de mise à jour (10s)\n")

while True:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"⏰ Mise à jour - {timestamp}")
    print(f"{'='*50}\n")

    print("📥 Téléchargement des flux temps réel...")
    siri_positions = fetch_siri_vehicle_positions() if SIRI_USE else None
    if siri_positions is not None:
        print(f"✅ {len(siri_positions)} positions SIRI reçues")
    elif SIRI_USE:
        print("⚠️ Pas de positions SIRI disponibles")

    trip_updates_feed = fetch_gtfs_realtime_feed(GTFS_RT_URL_TRIP_UPDATES, "GTFS trip-updates")
    positions_feed = fetch_gtfs_realtime_feed(GTFS_RT_URL, "GTFS vehicle-positions", cache_file="positions.pb")

    departure_times = {}
    departure_delays = {}
    departure_schedules = {}
    departure_sources = {}
    siri_vehicle_ids = set()
    if trip_updates_feed:
        for entity in trip_updates_feed.entity:
            if entity.HasField("trip_update"):
                trip_update = entity.trip_update
                trip_id = trip_update.trip.trip_id if trip_update.trip.trip_id else None
                if not trip_id:
                    continue

                scheduled = get_next_scheduled_departure(trip_id)
                selected_delay = None
                selected_timestamp = None
                selected_sequence = None
                for stu in trip_update.stop_time_update:
                    sequence = stu.stop_sequence if stu.HasField("stop_sequence") else 0
                    delay = None
                    if stu.HasField("departure") and stu.departure.HasField("delay"):
                        delay = stu.departure.delay
                    elif stu.HasField("arrival") and stu.arrival.HasField("delay"):
                        delay = stu.arrival.delay

                    if selected_sequence is None or sequence < selected_sequence:
                        selected_sequence = sequence
                        selected_delay = delay

                    if selected_timestamp is None:
                        if stu.HasField("departure") and stu.departure.HasField("time"):
                            selected_timestamp = stu.departure.time
                        elif stu.HasField("arrival") and stu.arrival.HasField("time"):
                            selected_timestamp = stu.arrival.time

                if scheduled is not None:
                    departure_schedules[trip_id] = scheduled['departure_time']
                    departure_sources[trip_id] = 'realtime'
                    if selected_delay is not None:
                        actual_seconds = scheduled['departure_seconds'] + selected_delay
                        departure_times[trip_id] = format_seconds_as_time(actual_seconds)
                        departure_delays[trip_id] = selected_delay
                    else:
                        departure_times[trip_id] = format_seconds_as_time(scheduled['departure_seconds'])
                elif selected_timestamp is not None:
                    departure_times[trip_id] = datetime.fromtimestamp(selected_timestamp).strftime("%H:%M")
                    departure_sources[trip_id] = 'realtime'

    # Construire les prochains passages par arrêt (statique et réel)
    static_arrivals = defaultdict(list)
    realtime_arrivals = defaultdict(list)
    now_seconds = get_current_seconds()

    # Statique: parcourir les horaires planifiés
    for trip_id, stops in scheduled_departures.items():
        route_id = trips_data.get(trip_id, {}).get('route_id')
        for s in stops:
            dep_sec = s.get('departure_seconds')
            if dep_sec is None:
                continue
            if dep_sec >= now_seconds:
                stop_id = s.get('stop_id')
                if not stop_id:
                    continue
                static_arrivals[stop_id].append({
                    'seconds': dep_sec,
                    'time': format_seconds_as_time(dep_sec),
                    'trip_id': trip_id,
                    'route_id': route_id
                })

    for stop_id in static_arrivals:
        static_arrivals[stop_id].sort(key=lambda x: x['seconds'])

    # Temps réel: parcourir les trip_update pour extraire estimations par arrêt
    if trip_updates_feed:
        today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for entity in trip_updates_feed.entity:
            if entity.HasField('trip_update'):
                trip_update = entity.trip_update
                trip_id = trip_update.trip.trip_id if trip_update.trip.trip_id else None
                route_id = trips_data.get(trip_id, {}).get('route_id') if trip_id else None
                for stu in trip_update.stop_time_update:
                    stop_id = stu.stop_id if stu.stop_id else None
                    if not stop_id:
                        continue
                    est_ts = None
                    if stu.HasField('departure') and stu.departure.HasField('time'):
                        est_ts = stu.departure.time
                    elif stu.HasField('arrival') and stu.arrival.HasField('time'):
                        est_ts = stu.arrival.time
                    else:
                        delay = None
                        if stu.HasField('departure') and stu.departure.HasField('delay'):
                            delay = stu.departure.delay
                        elif stu.HasField('arrival') and stu.arrival.HasField('delay'):
                            delay = stu.arrival.delay
                        if delay is not None and trip_id in scheduled_departures:
                            for s in scheduled_departures[trip_id]:
                                if s.get('stop_id') == stop_id and s.get('departure_seconds') is not None:
                                    est_ts = int(today_midnight + s['departure_seconds'] + delay)
                                    break
                    if est_ts is None:
                        continue
                    try:
                        est_dt = datetime.fromtimestamp(int(est_ts))
                        est_time = est_dt.strftime("%H:%M")
                        realtime_arrivals[stop_id].append({
                            'timestamp': int(est_ts),
                            'time': est_time,
                            'trip_id': trip_id,
                            'route_id': route_id
                        })
                    except Exception:
                        continue

    for stop_id in realtime_arrivals:
        realtime_arrivals[stop_id].sort(key=lambda x: x['timestamp'])

    m = folium.Map(location=[47.4784, -0.5632], zoom_start=12, tiles="CartoDB positron")
    print("📍 Affichage des trajets des lignes...")
    displayed_shapes = set()
    for trip_id, trip_info in trips_data.items():
        shape_id = trip_info.get('shape_id')
        route_id = trip_info.get('route_id')
        route_number = route_id_number(route_id)
        if route_number is not None and route_number > 42:
            continue
        if shape_id and shape_id in shapes_data and shape_id not in displayed_shapes:
            route_info = routes_data.get(route_id, {})
            style = get_route_style(route_info, route_id=route_id)
            folium.PolyLine(
                locations=shapes_data[shape_id],
                color=style['bg_color'],
                weight=4,
                opacity=0.7,
                popup=f"Ligne {route_id} ({style['route_type_name']})",
                tooltip=f"{style['route_emoji']} {style['route_type_name']} - Ligne {route_id}"
            ).add_to(m)
            displayed_shapes.add(shape_id)

    print(f"✅ {len(displayed_shapes)} trajets affichés")

    # Afficher les arrêts et popups avec les 2 prochains passages (statique + réel)
    for stop_id, coords in stops_coords.items():
        try:
            lat, lon = coords
        except Exception:
            continue
        stop_name = stops_data.get(stop_id) or stop_id
        stat = static_arrivals.get(stop_id, [])[:2]
        real = realtime_arrivals.get(stop_id, [])[:2]
        stat_html = ''
        if stat:
            for s in stat:
                route = s.get('route_id') or ''
                trip_id = s.get('trip_id')
                dest = ''
                if trip_id:
                    dest = trips_data.get(trip_id, {}).get('headsign', '')
                dest_display = f" → {dest}" if dest else ''
                stat_html += f"{s.get('time')} (L{route}){dest_display}<br>"
        else:
            stat_html = 'Aucun passage prévu<br>'
        real_html = ''
        if real:
            for r in real:
                route = r.get('route_id') or ''
                trip_id = r.get('trip_id')
                dest = ''
                if trip_id:
                    dest = trips_data.get(trip_id, {}).get('headsign', '')
                dest_display = f" → {dest}" if dest else ''
                real_html += f"{r.get('time')} (L{route}){dest_display}<br>"
        else:
            real_html = 'Aucune estimation temps réel<br>'

        # Afficher le réel si disponible, sinon afficher le statique
        if real:
            popup_html = f"<b>Arrêt :</b> {stop_name}<br><hr><b>Prochains passages :</b><br>{real_html}"
        else:
            popup_html = f"<b>Arrêt :</b> {stop_name}<br><hr><b>Prochains passages :</b><br>{stat_html}"
        folium.CircleMarker(
            location=[lat, lon],
            radius=4,
            color='#2A72B5',
            fill=True,
            fill_color='#2A72B5',
            fill_opacity=0.9,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=stop_name
        ).add_to(m)

    print("📍 Affichage des véhicules...")
    count = 0
    displayed_vehicle_types = []
    realtime_count = 0
    static_count = 0
    if positions_feed:
        for entity in positions_feed.entity:
            if entity.HasField("vehicle"):
                vehicle = entity.vehicle
                lat = vehicle.position.latitude
                lon = vehicle.position.longitude
                vehicle_id = vehicle.vehicle.id if vehicle.vehicle.id else "N/A"
                trip_id = vehicle.trip.trip_id if vehicle.trip.trip_id else "N/A"
                route_id = vehicle.trip.route_id if vehicle.trip.route_id else "N/A"
                route_number = route_id_number(route_id)
                if route_number is not None and route_number > 42:
                    continue
                bearing = vehicle.position.bearing if vehicle.position.HasField("bearing") else 0
                speed = vehicle.position.speed if vehicle.position.HasField("speed") else 0
                route_info = routes_data.get(route_id, {})
                style = get_route_style(route_info, route_id=route_id)
                route_name = route_info.get('name', route_id)
                # try to get destination from static trips data first, else from realtime trip descriptor
                dest = ''
                if trip_id and trip_id in trips_data:
                    dest = trips_data.get(trip_id, {}).get('headsign', '')
                if not dest:
                    try:
                        dest = vehicle.trip.trip_headsign
                    except Exception:
                        dest = dest or ''
                scheduled = get_next_scheduled_departure(trip_id)
                scheduled_time = departure_schedules.get(trip_id) or (scheduled and scheduled.get('departure_time'))
                next_stop_id = scheduled.get('stop_id') if scheduled else None
                next_stop_name = stops_data.get(next_stop_id) if next_stop_id else None
                departure_time = departure_times.get(trip_id)
                terminus_info = get_terminus_departure(trip_id)
                terminus_arrival_time = None
                terminus_stop_id = None
                terminus_stop_name = None
                if terminus_info:
                    terminus_arrival_time = terminus_info.get('arrival_time') or terminus_info.get('departure_time')
                    terminus_stop_id = terminus_info.get('stop_id')
                    terminus_stop_name = stops_data.get(terminus_stop_id) if terminus_stop_id else None
                
                # Skip vehicle if terminus arrival time has already passed
                if terminus_arrival_time:
                    terminus_seconds = parse_gtfs_time(terminus_arrival_time)
                    if terminus_seconds is not None and terminus_seconds < get_current_seconds():
                        continue

                vehicle_type = 'tram' if route_info.get('type') in [0, 5] else 'bus'
                
                if departure_time is None and scheduled is not None:
                    departure_time = format_seconds_as_time(scheduled['departure_seconds'])
                departure_source = departure_sources.get(trip_id) or ('static' if scheduled else 'N/A')
                delay_seconds = departure_delays.get(trip_id)
                delay_text = None
                if delay_seconds is not None:
                    delay_minutes = round(delay_seconds / 60)
                    prefix = '+' if delay_minutes >= 0 else ''
                    delay_text = f"{prefix}{delay_minutes} min"

                popup = f"""
                <b>Ligne :</b> {route_name}<br>
                <b>Type :</b> {style['route_emoji']} {style['route_type_name']}<br>
                <b>Véhicule :</b> {vehicle_id}<br>
                <b>Trip :</b> {trip_id}<br>
                <b>Destination :</b> {dest or 'N/A'}<br>
                <b>Source horaire :</b> {departure_source}<br>
                <b>Arrêt suivant :</b> {next_stop_name or next_stop_id or 'N/A'}<br>
                <b>Heure prévue :</b> {scheduled_time or 'N/A'}<br>
                <b>Heure estimée :</b> {departure_time or 'N/A'}<br>
                <b>Termin. :</b> {terminus_stop_name or terminus_stop_id or 'N/A'}<br>
                <b>Heure arrivée terminus :</b> {terminus_arrival_time or 'N/A'}<br>
                <b>Écart :</b> {delay_text or 'N/A'}<br>
                """

                icon = create_custom_icon(
                    route_name,
                    vehicle_id,
                    style,
                    destination=dest,
                    departure_time=departure_time,
                    departure_source=departure_source
                )
                folium.Marker(
                    location=[lat, lon],
                    popup=folium.Popup(popup, max_width=300),
                    tooltip=f"{style['route_emoji']} {style['route_type_name']} {route_name} - Véhicule {vehicle_id}",
                    icon=icon
                ).add_to(m)
                displayed_vehicle_types.append(vehicle_type)
                siri_vehicle_ids.add(vehicle_id)
                count += 1
                if departure_source == 'realtime':
                    realtime_count += 1
                elif departure_source == 'static':
                    static_count += 1

    if siri_positions:
        for vehicle in siri_positions:
            vehicle_id = vehicle.get('vehicle_id') or 'N/A'
            if vehicle_id in siri_vehicle_ids:
                continue
            lat = vehicle.get('latitude')
            lon = vehicle.get('longitude')
            if lat is None or lon is None:
                continue
            route_id = vehicle.get('route_id') or 'N/A'
            route_number = route_id_number(route_id)
            if route_number is not None and route_number > 42:
                continue
            route_info = routes_data.get(route_id, {})
            style = get_route_style(route_info, route_id=route_id)
            route_name = route_info.get('name', route_id)
            dest = vehicle.get('dest') or ''
            departure_time = vehicle.get('expected_time')
            departure_source = 'realtime-siri'
            delay_text = 'N/A'
            popup = f"""
            <b>Ligne :</b> {route_name}<br>
            <b>Type :</b> {style['route_emoji']} {style['route_type_name']}<br>
            <b>Véhicule :</b> {vehicle_id}<br>
            <b>Trip :</b> {vehicle.get('trip_id') or 'N/A'}<br>
            <b>Destination :</b> {dest or 'N/A'}<br>
            <b>Source horaire :</b> {departure_source}<br>
            <b>Heure estimée :</b> {departure_time or 'N/A'}<br>
            <b>Écart :</b> {delay_text or 'N/A'}<br>
            """
            icon = create_custom_icon(
                route_name,
                vehicle_id,
                style,
                destination=dest,
                departure_time=departure_time,
                departure_source=departure_source
            )
            folium.Marker(
                location=[lat, lon],
                popup=folium.Popup(popup, max_width=300),
                tooltip=f"{style['route_emoji']} {style['route_type_name']} {route_name} - Véhicule {vehicle_id} (SIRI)",
                icon=icon
            ).add_to(m)
            displayed_vehicle_types.append('bus' if route_info.get('type') not in [0,5] else 'tram')
            count += 1
            realtime_count += 1

    bus_count = displayed_vehicle_types.count('bus')
    tram_count = displayed_vehicle_types.count('tram')
    print(f"✅ {count} véhicules affichés ({bus_count} bus, {tram_count} trams)")
    print(f"   ⏱️ Horaires temps réel : {realtime_count}, statiques : {static_count}")
    legend_html = '''
    <div style="position: fixed; 
         bottom: 50px; right: 50px; width: 220px; min-height: 140px; 
         background-color: white; border:2px solid grey; z-index:9999; 
         font-size:14px; padding: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.2);">
         <p style="margin: 5px 0;"><b>Légende</b></p>
         <div style="margin: 6px 0; display: flex; align-items: center; gap: 8px;">
             <span style="display: inline-block; width: 16px; height: 16px; background: #4A90E2; border-radius: 10%;"></span>
             Bus
         </div>
         <div style="margin: 6px 0; display: flex; align-items: center; gap: 8px;">
             <span style="display: inline-block; width: 16px; height: 16px; background: #E94F37; border-radius: 50%;"></span>
             Tram
         </div>
         <div style="margin: 6px 0; display: flex; align-items: center; gap: 8px;">
             <span style="display: inline-block; width: 12px; height: 12px; margin: 2px; background: #2E8B57; transform: rotate(45deg);"></span>
             Car Suburbain
         </div>
         <p style="margin: 8px 0 0 0; font-size: 12px; color: gray;">Couleurs selon la ligne GTFS</p>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))
    m.save("irigo_map.html")

    print("╔════════════════════════════════════════╗")
    print("║  CARTE IRIGO  par Thierry Prunier       ")
    print("╚════════════════════════════════════════╝")
    print(f"✅ Total véhicules affichés : {count}")
    print(f"   🚌 Bus : {bus_count}")
    print(f"   🚊 Trams : {tram_count}")
    print(f"📍 Carte générée : irigo_map.html")
    print("════════════════════════════════════════")
    print("✅ Mise à jour terminée")
    print("\n⏳ Prochaine mise à jour dans 10 secondes...\n")
    time.sleep(10)
