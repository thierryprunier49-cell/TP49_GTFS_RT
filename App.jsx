import { useEffect, useMemo, useState } from "react";

const REFRESH_INTERVAL_MS = 10_000;
const STORAGE_KEYS = {
  stop: "gtfs-origin-stop",
  route: "gtfs-route-filter",
  direction: "gtfs-direction-filter",
};

async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Erreur HTTP ${response.status}`);
  }
  return payload;
}

function readPreference(key) {
  try {
    return window.localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

function savePreference(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // The application remains usable when browser storage is disabled.
  }
}

function App() {
  const [stops, setStops] = useState([]);
  const [selectedStop, setSelectedStop] = useState("");
  const [selectedRoute, setSelectedRoute] = useState(() =>
    readPreference(STORAGE_KEYS.route),
  );
  const [selectedDirection, setSelectedDirection] = useState(() =>
    readPreference(STORAGE_KEYS.direction),
  );
  const [departures, setDepartures] = useState([]);
  const [updatedAt, setUpdatedAt] = useState("");
  const [loadingStops, setLoadingStops] = useState(true);
  const [loadingDepartures, setLoadingDepartures] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    fetchJson("/api/stops")
      .then(({ stops: loadedStops, preferredStopId }) => {
        if (!active) return;
        setStops(loadedStops);
        const savedRoute = readPreference(STORAGE_KEYS.route);
        if (
          savedRoute &&
          !loadedStops.some((stop) => stop.routeNames.includes(savedRoute))
        ) {
          setSelectedRoute("");
        }
        const savedStop = readPreference(STORAGE_KEYS.stop);
        const stopId = loadedStops.some((stop) => stop.id === savedStop)
          ? savedStop
          : loadedStops.some((stop) => stop.id === preferredStopId)
            ? preferredStopId
            : loadedStops[0]?.id || "";
        setSelectedStop(stopId);
        setError("");
      })
      .catch((fetchError) => {
        if (active) setError(fetchError.message);
      })
      .finally(() => {
        if (active) setLoadingStops(false);
      });

    return () => {
      active = false;
    };
  }, []);

  const availableRoutes = useMemo(
    () => [...new Set(stops.flatMap((stop) => stop.routeNames))].sort((a, b) =>
      a.localeCompare(b, "fr"),
    ),
    [stops],
  );

  const visibleStops = useMemo(
    () =>
      selectedRoute
        ? stops.filter((stop) => stop.routeNames.includes(selectedRoute))
        : stops,
    [selectedRoute, stops],
  );

  useEffect(() => {
    if (!visibleStops.some((stop) => stop.id === selectedStop) && visibleStops.length) {
      setSelectedStop(visibleStops[0].id);
    }
  }, [selectedStop, visibleStops]);

  useEffect(() => {
    if (!selectedStop) return undefined;
    let active = true;
    let requestInProgress = false;

    const refresh = async () => {
      if (requestInProgress) return;
      requestInProgress = true;
      setLoadingDepartures(true);
      try {
        const payload = await fetchJson(
          `/api/departures?stop=${encodeURIComponent(selectedStop)}`,
        );
        if (!active) return;
        setDepartures(payload.departures);
        setUpdatedAt(payload.updatedAt);
        setError("");
      } catch (fetchError) {
        if (active) setError(fetchError.message);
      } finally {
        requestInProgress = false;
        if (active) setLoadingDepartures(false);
      }
    };

    refresh();
    const timer = window.setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [selectedStop]);

  useEffect(() => savePreference(STORAGE_KEYS.stop, selectedStop), [selectedStop]);
  useEffect(() => savePreference(STORAGE_KEYS.route, selectedRoute), [selectedRoute]);
  useEffect(
    () => savePreference(STORAGE_KEYS.direction, selectedDirection),
    [selectedDirection],
  );

  const filteredDepartures = departures.filter(
    (departure) =>
      (!selectedRoute || departure.routeShortName === selectedRoute) &&
      (!selectedDirection || departure.directionId === selectedDirection),
  );

  return (
    <main className="app-shell">
      <header className="page-header">
        <div>
          <p className="eyebrow">Réseau Irigo</p>
          <h1>Prochains passages</h1>
        </div>
        <span className={`live-indicator ${error ? "is-error" : ""}`}>
          <span aria-hidden="true" />
          {error ? "Données indisponibles" : "Actualisation automatique"}
        </span>
      </header>

      <section className="filters" aria-label="Filtres des passages">
        <label className="filter-field stop-field">
          <span>Arrêt</span>
          <select
            value={selectedStop}
            onChange={(event) => setSelectedStop(event.target.value)}
            disabled={loadingStops || stops.length === 0}
          >
            {loadingStops && <option value="">Chargement des arrêts…</option>}
            {visibleStops.map((stop) => (
              <option key={stop.id} value={stop.id}>
                {stop.name}
              </option>
            ))}
          </select>
        </label>

        <label className="filter-field">
          <span>Ligne</span>
          <select
            value={selectedRoute}
            onChange={(event) => setSelectedRoute(event.target.value)}
          >
            <option value="">Toutes les lignes</option>
            {availableRoutes.map((route) => (
              <option key={route} value={route}>
                Ligne {route}
              </option>
            ))}
          </select>
        </label>

        <label className="filter-field">
          <span>Direction</span>
          <select
            value={selectedDirection}
            onChange={(event) => setSelectedDirection(event.target.value)}
          >
            <option value="">Toutes</option>
            <option value="1">Centre-ville</option>
            <option value="0">Extérieur</option>
          </select>
        </label>
      </section>

      <section className="results" aria-live="polite">
        <div className="results-heading">
          <h2>{stops.find((stop) => stop.id === selectedStop)?.name || "Passages"}</h2>
          <span>
            {updatedAt
              ? `Mis à jour à ${new Intl.DateTimeFormat("fr-FR", {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                  timeZone: "Europe/Paris",
                }).format(new Date(updatedAt))}`
              : loadingDepartures
                ? "Chargement…"
                : ""}
          </span>
        </div>

        {error && (
          <p className="error-message" role="alert">
            {error}
          </p>
        )}

        {loadingDepartures && filteredDepartures.length === 0 && !error && (
          <p className="empty-state">Chargement des prochains passages…</p>
        )}

        {!loadingDepartures && filteredDepartures.length === 0 && !error && (
          <p className="empty-state">Aucun prochain passage pour ces filtres.</p>
        )}

        <div className="departure-list">
          {filteredDepartures.map((departure) => {
            const routeColor = /^#[\da-f]{6}$/i.test(departure.routeColor)
              ? departure.routeColor
              : /^([\da-f]{6})$/i.test(departure.routeColor)
                ? `#${departure.routeColor}`
                : "#444444";
            const isRealtime = departure.source === "GTFS-RT";

            return (
              <article className="departure-card" key={departure.tripId}>
                <div className="route-badge" style={{ backgroundColor: routeColor }}>
                  {departure.routeShortName}
                </div>
                <div className="departure-destination">
                  <strong>{departure.destinationName}</strong>
                  <span>
                    {departure.directionId === "1"
                      ? "Centre-ville"
                      : departure.directionId === "0"
                        ? "Extérieur"
                        : "Direction"}
                  </span>
                </div>
                <div className="departure-time">
                  <strong>{departure.departureTime}</strong>
                  {!isRealtime && <span>horaire planifié</span>}
                </div>
              </article>
            );
          })}
        </div>
      </section>

      <footer className="page-footer">
        {updatedAt && (
          <span>
            Actualisé le{" "}
            {new Intl.DateTimeFormat("fr-FR", {
              dateStyle: "short",
              timeStyle: "short",
              timeZone: "Europe/Paris",
            }).format(new Date(updatedAt))}
          </span>
        )}
        <span>* horaire statique (planifié)</span>
      </footer>
    </main>
  );
}

export default App;
