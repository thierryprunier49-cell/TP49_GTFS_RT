#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import io
import os
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
import requests
from google.transit import gtfs_realtime_pb2

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

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
AUTO_REFRESH_SECONDS = 15
MAX_VEHICLE_POSITION_AGE_SECONDS = 60
MAX_STOP_ARRIVAL_AHEAD_SECONDS = 3 * 60 * 60
PARIS_TIMEZONE = ZoneInfo("Europe/Paris")
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(DATA_DIR, "irigo_map.html")
CACHE_POSITIONS_FILE = os.path.join(DATA_DIR, "positions.pb")

routes_data = {}
shapes_data = defaultdict(list)
trips_data = {}
stops_data = {}
stops_coords = {}
scheduled_departures = defaultdict(list)
calendar_data = {}
calendar_dates_data = defaultdict(list)

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

        latitude = _find_text(vehicle_location, 'Latitude')
        longitude = _find_text(vehicle_location, 'Longitude')
        bearing = _find_text(vehicle_location, 'Bearing')
        speed = _find_text(vehicle_location, 'Speed')

        vehicle_id = _find_text(mvj, 'VehicleRef') or _find_text(mvj, 'VehicleID') or 'N/A'
        route_id = _find_text(mvj, 'LineRef') or 'N/A'
        dest = _find_text(mvj, 'DestinationName') or ''
        trip_id = _find_text(mvj, 'DatedVehicleJourneyRef') or _find_text(mvj, 'FramedVehicleJourneyRef') or 'N/A'
        expected = _find_text(mvj, 'ExpectedDepartureTime') or _find_text(mvj, 'ExpectedArrivalTime')

        def _safe_float(value):
            try:
                return float(value) if value else None
            except Exception:
                return None

        vehicles.append({
            'vehicle_id': vehicle_id,
            'route_id': route_id,
            'trip_id': trip_id,
            'dest': dest,
            'latitude': _safe_float(latitude),
            'longitude': _safe_float(longitude),
            'bearing': _safe_float(bearing),
            'speed': _safe_float(speed),
            'expected_time': expected,
        })
    return vehicles


def fetch_siri_vehicle_positions(session):
    if not SIRI_USE:
        return None
    try:
        body = build_siri_vehicle_monitoring_request()
        siri_headers = {**HEADERS, 'Content-Type': 'application/xml'}
        if SIRI_ACCESS_TOKEN:
            siri_headers['Authorization'] = SIRI_ACCESS_TOKEN
        elif SIRI_API_KEY:
            siri_headers[SIRI_API_KEY_HEADER] = SIRI_API_KEY
        response = session.post(SIRI_URL, headers=siri_headers, data=body.encode('utf-8'), timeout=15)
        response.raise_for_status()
        return parse_siri_vehicle_monitoring(response.content)
    except Exception as exc:
        print(f"⚠️  Erreur SIRI : {exc}")
        return None


def parse_gtfs_time(time_str):
    try:
        hours, minutes, seconds = time_str.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    except Exception:
        return None


def is_service_active_today(service_id):
    """
    Vérifier si un service est actif aujourd'hui en utilisant le calendrier GTFS.
    Prend en compte calendar.txt et calendar_dates.txt
    """
    if not service_id:
        return True  # Si pas de service_id, on suppose que c'est actif
    
    today = datetime.now(PARIS_TIMEZONE).date()
    today_str = today.strftime("%Y%m%d")
    day_name = today.strftime("%A").lower()
    day_mapping = {
        'monday': 'monday',
        'tuesday': 'tuesday',
        'wednesday': 'wednesday',
        'thursday': 'thursday',
        'friday': 'friday',
        'saturday': 'saturday',
        'sunday': 'sunday',
    }
    gtfs_day = day_mapping.get(day_name, 'monday')
    
    # Vérifier les exceptions calendar_dates en premier
    if service_id in calendar_dates_data:
        for exception in calendar_dates_data[service_id]:
            if exception['date'] == today_str:
                return exception['service_available'] == '1'
    
    # Vérifier le calendrier régulier
    if service_id in calendar_data:
        cal = calendar_data[service_id]
        start_date = datetime.strptime(cal['start_date'], "%Y%m%d").date()
        end_date = datetime.strptime(cal['end_date'], "%Y%m%d").date()
        
        if not (start_date <= today <= end_date):
            return False
        
        day_field_mapping = {
            'monday': 'monday',
            'tuesday': 'tuesday',
            'wednesday': 'wednesday',
            'thursday': 'thursday',
            'friday': 'friday',
            'saturday': 'saturday',
            'sunday': 'sunday',
        }
        day_field = day_field_mapping.get(gtfs_day, 'monday')
        return cal.get(day_field, '0') == '1'
    
    return True





def format_seconds_as_time(seconds):
    seconds = int(seconds) % 86400
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def get_current_seconds():
    now = datetime.now(PARIS_TIMEZONE)
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


def get_terminus_info(trip_id, delay_seconds=None):
    terminus = get_terminus_departure(trip_id)
    if not terminus:
        return None
    terminus_stop_id = terminus.get('stop_id')
    terminus_stop_name = stops_data.get(terminus_stop_id) if terminus_stop_id else None
    terminus_time = terminus.get('arrival_time') or terminus.get('departure_time')
    if not terminus_time:
        return {
            'stop_name': terminus_stop_name,
            'stop_id': terminus_stop_id,
            'arrival_time': None,
        }
    terminus_seconds = parse_gtfs_time(terminus_time)
    if terminus_seconds is not None and delay_seconds is not None:
        terminus_seconds += delay_seconds
        terminus_time = format_seconds_as_time(terminus_seconds)
    return {
        'stop_name': terminus_stop_name,
        'stop_id': terminus_stop_id,
        'arrival_time': terminus_time,
    }


def load_gtfs_static_data(session):
    try:
        response = session.get(GTFS_STATIC_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()
        archive = zipfile.ZipFile(io.BytesIO(response.content))
    except Exception as exc:
        print(f"⚠️  Erreur GTFS statique : {exc}")
        return

    def read_table(filename, callback):
        try:
            with archive.open(filename) as f:
                callback(csv.DictReader(io.TextIOWrapper(f, encoding='utf-8')))
        except KeyError:
            print(f"⚠️  Fichier {filename} non trouvé")

    def load_routes(reader):
        for row in reader:
            route_id = row['route_id']
            routes_data[route_id] = {
                'name': row.get('route_short_name', row.get('route_long_name', route_id)),
                'type': int(row.get('route_type', 3) or 3),
                'color': row.get('route_color', 'FF0000')
            }

    def load_shapes(reader):
        for row in reader:
            shape_id = row['shape_id']
            try:
                lat = float(row['shape_pt_lat'])
                lon = float(row['shape_pt_lon'])
                shapes_data[shape_id].append([lat, lon])
            except Exception:
                continue

    def load_trips(reader):
        for row in reader:
            trip_id = row['trip_id']
            trips_data[trip_id] = {
                'route_id': row['route_id'],
                'shape_id': row.get('shape_id'),
                'headsign': row.get('trip_headsign', ''),
                'service_id': row.get('service_id', ''),
            }

    def load_stops(reader):
        for row in reader:
            stop_id = row['stop_id']
            stops_data[stop_id] = row.get('stop_name') or row.get('stop_desc') or stop_id
            try:
                stops_coords[stop_id] = (float(row['stop_lat']), float(row['stop_lon']))
            except Exception:
                pass

    def load_stop_times(reader):
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
                'stop_id': row.get('stop_id', ''),
            })

    def load_calendar(reader):
        for row in reader:
            service_id = row['service_id']
            calendar_data[service_id] = {
                'monday': row.get('monday', '0'),
                'tuesday': row.get('tuesday', '0'),
                'wednesday': row.get('wednesday', '0'),
                'thursday': row.get('thursday', '0'),
                'friday': row.get('friday', '0'),
                'saturday': row.get('saturday', '0'),
                'sunday': row.get('sunday', '0'),
                'start_date': row.get('start_date', ''),
                'end_date': row.get('end_date', ''),
            }

    def load_calendar_dates(reader):
        for row in reader:
            service_id = row['service_id']
            calendar_dates_data[service_id].append({
                'date': row.get('date', ''),
                'service_available': row.get('exception_type', '0'),
            })

    read_table('routes.txt', load_routes)
    read_table('shapes.txt', load_shapes)
    read_table('trips.txt', load_trips)
    read_table('stops.txt', load_stops)
    read_table('stop_times.txt', load_stop_times)
    read_table('calendar.txt', load_calendar)
    read_table('calendar_dates.txt', load_calendar_dates)

    for stop_list in scheduled_departures.values():
        stop_list.sort(key=lambda x: ((x['departure_seconds'] if x['departure_seconds'] is not None else x['arrival_seconds']), x['stop_sequence']))

    print(f"✅ {len(routes_data)} lignes chargées")
    print(f"✅ {len(shapes_data)} trajets chargés")
    print(f"✅ {len(trips_data)} voyages chargés")
    print(f"✅ {len(stops_data)} arrêts chargés")
    print(f"✅ {len(scheduled_departures)} horaires de départ chargés")
    print(f"✅ {len(calendar_data)} calendriers chargés")
    if calendar_dates_data:
        print(f"✅ {len(calendar_dates_data)} dates d'exceptions chargées")


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
        color_hex = '#' + ''.join(c * 2 for c in color_hex.lstrip('#'))

    def get_text_color(hex_color):
        rgb = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
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


def create_custom_icon(
    route_name,
    vehicle_id,
    style,
    destination="",
    departure_source='N/A',
):
    route_display = str(route_name)[:4]
    vehicle_display = str(vehicle_id)[-4:] if vehicle_id != 'N/A' else ''
    dest = str(destination) if destination is not None else ''
    dest_display = dest if dest and len(dest) <= 12 else (dest[:11] + '…' if dest else vehicle_display)
    shape = style['icon_shape']
    bg_color = style['bg_color']
    text_color = style['text_color']

    source_border_colors = {
        'realtime': '#34A853',
        'realtime-siri': '#1E88E5',
        'realtime-tripupdate': '#FB8C00',
        'static': '#F4B400',
        'N/A': '#888888',
    }
    border_color = source_border_colors.get(departure_source, '#888888')

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
        </div>
    </div>
    """
    return folium.DivIcon(html=html)


def is_fresh_vehicle_position(vehicle, now=None):
    if not vehicle.HasField('timestamp'):
        return False
    reference_time = time.time() if now is None else now
    age_seconds = reference_time - vehicle.timestamp
    return -30 <= age_seconds <= MAX_VEHICLE_POSITION_AGE_SECONDS


def fetch_gtfs_realtime_feed(session, url, label, cache_file=None):
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        response = session.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        feed.ParseFromString(response.content)
        print(f"✅ Données {label} reçues ({len(feed.entity)} entités)")
        if cache_file:
            with open(cache_file, 'wb') as f:
                f.write(response.content)
        return feed
    except Exception as exc:
        print(f"⚠️  Erreur {label} : {exc}")
        if cache_file and os.path.exists(cache_file):
            try:
                with open(cache_file, 'rb') as f:
                    feed.ParseFromString(f.read())
                print(f"✅ Fichier local chargé pour {label}")
                return feed
            except Exception as exc2:
                print(f"⚠️  Impossible de charger le cache local pour {label} : {exc2}")
        return None


def build_arrivals(trip_updates_feed):
    static_arrivals = defaultdict(list)
    realtime_arrivals = defaultdict(list)
    now = datetime.now(PARIS_TIMEZONE)
    now_seconds = get_current_seconds()
    now_timestamp = now.timestamp()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    for trip_id, stops in scheduled_departures.items():
        trip_info = trips_data.get(trip_id, {})
        service_id = trip_info.get('service_id', '')
        
        # Vérifier si le service est actif aujourd'hui
        if not is_service_active_today(service_id):
            continue
        
        route_id = trip_info.get('route_id')
        for s in stops:
            dep_sec = s.get('departure_seconds')
            if dep_sec is None or dep_sec < now_seconds:
                continue
            stop_id = s.get('stop_id')
            if not stop_id:
                continue
            static_arrivals[stop_id].append({
                'seconds': dep_sec,
                'timestamp': int(today_midnight + dep_sec),
                'time': format_seconds_as_time(dep_sec),
                'trip_id': trip_id,
                'route_id': route_id,
            })

    for arrivals in static_arrivals.values():
        arrivals.sort(key=lambda x: x['seconds'])

    for entity in trip_updates_feed.entity if trip_updates_feed else ():
        if not entity.HasField('trip_update'):
            continue
        trip_update = entity.trip_update
        trip_id = trip_update.trip.trip_id if trip_update.trip.trip_id else None
        if not trip_id:
            continue
        
        # Vérifier si le service est actif aujourd'hui
        trip_info = trips_data.get(trip_id, {})
        service_id = trip_info.get('service_id', '')
        if not is_service_active_today(service_id):
            continue
        
        for stu in trip_update.stop_time_update:
            stop_id = stu.stop_id if stu.stop_id else None
            if not stop_id or stop_id not in stops_coords:
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
            if not now_timestamp <= est_ts <= (
                now_timestamp + MAX_STOP_ARRIVAL_AHEAD_SECONDS
            ):
                continue
            try:
                est_dt = datetime.fromtimestamp(int(est_ts), PARIS_TIMEZONE)
                realtime_arrivals[stop_id].append({
                    'timestamp': int(est_ts),
                    'time': est_dt.strftime("%H:%M"),
                    'trip_id': trip_id,
                    'route_id': trips_data.get(trip_id, {}).get('route_id'),
                })
            except Exception:
                continue

    for arrivals in realtime_arrivals.values():
        arrivals.sort(key=lambda x: x['timestamp'])

    for stop_id in set(static_arrivals) | set(realtime_arrivals):
        arrivals = realtime_arrivals[stop_id]
        realtime_trip_ids = {arrival['trip_id'] for arrival in arrivals}
        arrivals.extend(
            arrival
            for arrival in static_arrivals.get(stop_id, [])
            if arrival['trip_id'] not in realtime_trip_ids
        )
        arrivals.sort(key=lambda arrival: arrival['timestamp'])

    return static_arrivals, realtime_arrivals


def build_map(
    siri_positions,
    positions_feed,
    static_arrivals,
    realtime_arrivals,
):
    m = folium.Map(
        location=[47.4784, -0.5632],
        zoom_start=12,
        tiles="OpenStreetMap",
    )
    back_button_html = """
    <a href="/" aria-label="Retour aux prochains passages"
       style="position: fixed; top: 12px; left: 55px; z-index: 10000;
              padding: 9px 14px; border: 1px solid #b8c2cc; border-radius: 8px;
              background: white; color: #245a91; font: 600 14px sans-serif;
              text-decoration: none; box-shadow: 0 2px 8px rgba(0,0,0,.2);">
      ← Retour aux prochains passages
    </a>
    """
    m.get_root().html.add_child(folium.Element(back_button_html))
    print("📍 Affichage des trajets des lignes...")
    displayed_shapes = set()
    for trip_id, trip_info in trips_data.items():
        shape_id = trip_info.get('shape_id')
        route_id = trip_info.get('route_id')
        service_id = trip_info.get('service_id', '')
        
        # Vérifier si le service est actif aujourd'hui
        if not is_service_active_today(service_id):
            continue
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
                tooltip=f"{style['route_emoji']} {style['route_type_name']} - Ligne {route_id}",
            ).add_to(m)
            displayed_shapes.add(shape_id)

    print(f"✅ {len(displayed_shapes)} trajets affichés")

    for stop_id, coords in stops_coords.items():
        if not coords:
            continue
        stop_name = stops_data.get(stop_id) or stop_id
        arrivals = realtime_arrivals.get(stop_id, [])[:2]
        arrivals_html = ''
        for arrival in arrivals:
            route = arrival.get('route_id') or ''
            trip_id = arrival.get('trip_id')
            dest = trips_data.get(trip_id, {}).get('headsign', '') if trip_id else ''
            dest_display = f" → {dest}" if dest else ''
            arrivals_html += f"{arrival['time']} (L{route}){dest_display}<br>"
        if not arrivals_html:
            arrivals_html = 'Aucun passage prochainement<br>'
        popup_html = (
            f"<b>Arrêt :</b> {stop_name}<br><hr><b>Prochains passages :</b><br>"
            f"{arrivals_html}"
        )
        folium.CircleMarker(
            location=[coords[0], coords[1]],
            radius=4,
            color='#2A72B5',
            fill=True,
            fill_color='#2A72B5',
            fill_opacity=0.9,
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=stop_name,
        ).add_to(m)

    print("📍 Affichage des véhicules...")
    count = 0
    displayed_vehicle_types = []
    realtime_count = 0
    stale_position_count = 0

    def add_marker(
        lat,
        lon,
        route_id,
        route_name,
        trip_id,
        vehicle_id,
        dest,
        departure_source,
        popup,
    ):
        nonlocal count, realtime_count
        style = get_route_style(routes_data.get(route_id, {}), route_id=route_id)
        icon = create_custom_icon(
            route_name,
            vehicle_id,
            style,
            destination=dest,
            departure_source=departure_source,
        )
        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(popup, max_width=300),
            tooltip=f"{style['route_emoji']} {style['route_type_name']} {route_name} - Véhicule {vehicle_id}",
            icon=icon,
        ).add_to(m)
        displayed_vehicle_types.append('tram' if style['route_type_name'] == 'Tram' else 'bus')
        count += 1
        if departure_source.startswith('realtime'):
            realtime_count += 1

    if positions_feed:
        positions_checked_at = time.time()
        for entity in positions_feed.entity:
            if not entity.HasField('vehicle'):
                continue
            vehicle = entity.vehicle
            if not is_fresh_vehicle_position(vehicle, positions_checked_at):
                stale_position_count += 1
                continue
            vehicle_id = vehicle.vehicle.id.strip()
            if not vehicle_id or vehicle_id.upper() == 'N/A':
                continue
            lat = vehicle.position.latitude
            lon = vehicle.position.longitude
            trip_id = vehicle.trip.trip_id if vehicle.trip.trip_id else 'N/A'
            route_id = vehicle.trip.route_id if vehicle.trip.route_id else 'N/A'
            route_number = route_id_number(route_id)
            if route_number is not None and route_number > 42:
                continue
            
            # Vérifier si le service est actif aujourd'hui
            trip_info = trips_data.get(trip_id, {})
            service_id = trip_info.get('service_id', '')
            if not is_service_active_today(service_id):
                continue
            
            route_info = routes_data.get(route_id, {})
            route_name = route_info.get('name', route_id)
            dest = trips_data.get(trip_id, {}).get('headsign', '') if trip_id else ''
            if not dest:
                dest = getattr(vehicle.trip, 'trip_headsign', '') or ''
            position_age_seconds = max(
                0,
                int(positions_checked_at - vehicle.timestamp),
            )
            style = get_route_style(route_info, route_id=route_id)
            popup = f"""
            <b>Ligne :</b> {route_name}<br>
            <b>Type :</b> {style['route_emoji']} {style['route_type_name']}<br>
            <b>Véhicule :</b> {vehicle_id}<br>
            <b>Trip :</b> {trip_id}<br>
            <b>Dernière position GPS :</b> il y a {position_age_seconds} s<br>
            <b>Destination :</b> {dest or 'N/A'}<br>
            """
            add_marker(
                lat,
                lon,
                route_id,
                route_name,
                trip_id,
                vehicle_id,
                dest,
                'realtime',
                popup,
            )

    if stale_position_count:
        print(
            f"⚠️ {stale_position_count} positions GTFS-RT ignorées "
            f"(sans horodatage ou âgées de plus de "
            f"{MAX_VEHICLE_POSITION_AGE_SECONDS} secondes)"
        )

    if siri_positions:
        for vehicle in siri_positions:
            vehicle_id = vehicle.get('vehicle_id') or 'N/A'
            if not str(vehicle_id).strip() or str(vehicle_id).strip().upper() == 'N/A':
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
            route_name = route_info.get('name', route_id)
            dest = vehicle.get('dest') or ''
            siri_trip_id = vehicle.get('trip_id')
            style = get_route_style(route_info, route_id=route_id)
            popup = f"""
            <b>Ligne :</b> {route_name}<br>
            <b>Type :</b> {style['route_emoji']} {style['route_type_name']}<br>
            <b>Véhicule :</b> {vehicle_id}<br>
            <b>Trip :</b> {siri_trip_id or 'N/A'}<br>
            <b>Destination :</b> {dest or 'N/A'}<br>
            """
            add_marker(
                lat,
                lon,
                route_id,
                route_name,
                siri_trip_id or vehicle_id,
                vehicle_id,
                dest,
                'realtime-siri',
                popup,
            )

    bus_count = displayed_vehicle_types.count('bus')
    tram_count = displayed_vehicle_types.count('tram')
    print(f"✅ {count} véhicules affichés ({bus_count} bus, {tram_count} trams)")
    print(f"   ⏱️ Véhicules avec position temps réel : {realtime_count}")

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

    refresh_html = f'''
    <script>
      (function() {{
        var storageKey = 'irigo_map_state';

        function getMapInstance() {{
          if (window.L && window.L.Map) {{
            for (var key in window) {{
              if (window[key] instanceof window.L.Map) {{
                return window[key];
              }}
            }}
          }}
          return null;
        }}

        function restoreState(map) {{
          try {{
            var saved = localStorage.getItem(storageKey);
            if (!saved) {{
              return;
            }}
            var state = JSON.parse(saved);
            if (state && state.center && typeof state.zoom === 'number') {{
              map.setView([state.center.lat, state.center.lng], state.zoom);
            }}
            if (state && typeof state.scrollX === 'number' && typeof state.scrollY === 'number') {{
              window.scrollTo(state.scrollX, state.scrollY);
            }}
          }} catch (e) {{}}
        }}

        function saveState(map) {{
          try {{
            var center = map.getCenter();
            var zoom = map.getZoom();
            localStorage.setItem(storageKey, JSON.stringify({{
              center: center,
              zoom: zoom,
              scrollX: window.scrollX || 0,
              scrollY: window.scrollY || 0
            }}));
          }} catch (e) {{}}
        }}

        function init() {{
          var map = getMapInstance();
          if (!map) {{
            return;
          }}
          restoreState(map);
          map.on('moveend zoomend', function() {{
            saveState(map);
          }});
          window.addEventListener('scroll', function() {{
            saveState(map);
          }});
          window.addEventListener('beforeunload', function() {{
            saveState(map);
          }});
          window.addEventListener('pagehide', function() {{
            saveState(map);
          }});
          setTimeout(function() {{
            window.location.reload(true);
          }}, {AUTO_REFRESH_SECONDS * 1000});
        }}

        if (document.readyState === 'loading') {{
          document.addEventListener('DOMContentLoaded', init);
        }} else {{
          init();
        }}
      }})();
    </script>
    '''
    m.get_root().html.add_child(folium.Element(refresh_html))

    return m


def save_map(map_object, output_file):
    output_directory = os.path.dirname(os.path.abspath(output_file))
    file_descriptor, temporary_file = tempfile.mkstemp(
        prefix=f".{os.path.basename(output_file)}.",
        suffix=".tmp",
        dir=output_directory,
    )
    os.close(file_descriptor)
    try:
        map_object.save(temporary_file)
        os.replace(temporary_file, output_file)
    finally:
        if os.path.exists(temporary_file):
            os.unlink(temporary_file)
    print(f"📍 Carte générée : {output_file}")


def load_data():
    session = requests.Session()
    while True:
        routes_data.clear()
        shapes_data.clear()
        trips_data.clear()
        stops_data.clear()
        stops_coords.clear()
        scheduled_departures.clear()
        calendar_data.clear()
        calendar_dates_data.clear()
        try:
            load_gtfs_static_data(session)
        except Exception as error:
            print(f"❌ Erreur au chargement du GTFS statique : {error}")
        if routes_data and stops_coords:
            return session
        print(
            "❌ Impossible de charger les données GTFS statiques. "
            "Nouvel essai dans 30 secondes."
        )
        time.sleep(30)


def main():
    session = load_data()
    if not SIRI_USE:
        print("ℹ️  SIRI désactivé")

    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'='*50}")
        print(f"⏰ Mise à jour - {timestamp}")
        print(f"{'='*50}\n")

        siri_positions = fetch_siri_vehicle_positions(session)
        if siri_positions is not None:
            print(f"✅ {len(siri_positions)} positions SIRI reçues")
        elif SIRI_USE:
            print("⚠️ Pas de positions SIRI disponibles")

        trip_updates_feed = fetch_gtfs_realtime_feed(session, GTFS_RT_URL_TRIP_UPDATES, "GTFS trip-updates")
        positions_feed = fetch_gtfs_realtime_feed(session, GTFS_RT_URL, "GTFS vehicle-positions", cache_file=CACHE_POSITIONS_FILE)

        static_arrivals, realtime_arrivals = build_arrivals(trip_updates_feed)

        map_object = build_map(
            siri_positions,
            positions_feed,
            static_arrivals,
            realtime_arrivals,
        )
        save_map(map_object, OUTPUT_FILE)

        print("✅ Mise à jour terminée")
        print(f"\n⏳ Prochaine mise à jour dans {AUTO_REFRESH_SECONDS} secondes...\n")
        time.sleep(AUTO_REFRESH_SECONDS)


if __name__ == '__main__':
    main()
