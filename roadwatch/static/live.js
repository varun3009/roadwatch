import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from './vendor/vue.esm-browser.prod.js';
import { api, dashboard, datasetName, go, notify, requestReplay } from './shared.js';
import { HazardReportActions } from './hazard-report.js';

const sourceFields = {
  camera: ['CAMERA INDEX', '0'],
  stream: ['STREAM URL', 'rtsp://camera-address/stream'],
  file: ['LOCAL VIDEO PATH', 'E:\\path\\to\\recording.mp4'],
};
const statusText = { idle: 'Ready to inspect', running: 'Detection running', stopped: 'Session stopped', completed: 'Recording complete', error: 'Session error' };
const videoStatus = { running: 'LIVE', stopped: 'STOPPED', completed: 'COMPLETE', error: 'ERROR' };
const numeric = (value) => value !== null && value !== '' && value !== undefined && Number.isFinite(Number(value));
const decimal = (value, digits = 1) => numeric(value) ? Number(value).toFixed(digits) : '—';
const clock = (value) => {
  if (!numeric(value)) return '—';
  const seconds = Math.max(0, Math.floor(Number(value)));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
};
const latLng = (item) => {
  const { latitude, longitude } = item || {};
  return numeric(latitude) && numeric(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180
    ? [Number(latitude), Number(longitude)] : null;
};
const hazardName = (item) => String(item.class_name ?? item.hazard_type ?? item.type ?? 'Unknown')
  .replace(/[_-]/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
const isMatch = (item) => Boolean(item.match && item.match.matched !== false && (item.match.reference_id || item.match.reference || item.match.match_kind));
const confidence = (item) => Math.max(0, Math.min(100, Math.round((Number(item.confidence) || 0) * 100)));
const recordingDataset = (recording) => recording?.dataset_id || 'roadsens';
const stopGpsHelp = 'Connect a GPS bridge to POST /api/pose, or use browser GPS. Enter a vehicle heading if the device cannot report one.';
const sample = (points, maximum) => {
  const step = Math.max(1, Math.ceil(points.length / maximum));
  return points.filter((_, index) => index % step === 0);
};

export default {
  components: { HazardReportActions },
  setup() {
    const sourceType = ref('dataset');
    const sourcePath = ref('0');
    const poseMode = ref('dataset');
    const poseInputs = reactive({ latitude: '', longitude: '', heading_deg: '', accuracy_m: 5 });
    const operation = ref('');
    const gpsWatching = ref(false);
    const gpsMessage = ref('');
    const hazardFilter = ref('all');
    const follow = ref(true);
    const probing = ref(false);
    const probeResult = ref('');
    const disconnected = ref(false);
    const mapElement = ref(null);
    const mapOffline = ref('');
    let gpsWatch = null, latestGps = null, lastGpsSend = 0, loadVersion = 0;
    let map, routeLayer, referenceLayer, hazardLayer, vehicle, tileTimer, mapTimer, modelTimer;
    let receivedTiles = false;
    const markers = new Map();

    const state = computed(() => dashboard.state || {});
    const running = computed(() => state.value.status === 'running');
    const selectedId = computed(() => String(dashboard.selectedRecording || ''));
    const playableRecordings = computed(() => (dashboard.recordings || []).filter((record) => record.video_path));
    const visibleRecordings = computed(() => playableRecordings.value.filter((record) =>
      !dashboard.datasetFilter || dashboard.datasetFilter === 'all' || recordingDataset(record) === dashboard.datasetFilter));
    const details = computed(() => {
      const value = dashboard.selectedRecordingDetails;
      return String(value?.recording?.id) === selectedId.value ? value : null;
    });
    const selected = computed(() => sourceType.value === 'dataset' ? details.value?.recording ||
      playableRecordings.value.find((recording) => String(recording.id) === selectedId.value) : null);
    const comma = computed(() => Boolean(selected.value && recordingDataset(selected.value) === 'comma2k19'));
    const unlabeled = computed(() => Boolean(selected.value && (details.value?.has_labels === false || selected.value.has_labels === false || comma.value)));
    const verified = computed(() => !comma.value && Boolean(dashboard.config?.calibration?.verified));
    const track = computed(() => sourceType.value === 'dataset' ? details.value?.track || [] : []);
    const routePoints = computed(() => sample(track.value, 4000).map(latLng).filter(Boolean));
    const references = computed(() => {
      if (sourceType.value === 'dataset') return details.value?.references || [];
      return (dashboard.references || []).filter((item) => item.location_kind !== 'vehicle_at_annotation');
    });
    const referencePoints = computed(() => references.value.slice(0, 2000)
      .map((item) => ({ item, point: latLng(item) })).filter(({ point }) => point));
    const hazards = computed(() => state.value.hazards || []);
    const detections = computed(() => state.value.detections || []);
    const posePoint = computed(() => latLng(state.value.pose));
    const located = computed(() => hazards.value.map((item) => ({ item, point: latLng(item) })).filter(({ point }) => point));
    const filteredHazards = computed(() => hazards.value.slice().reverse().filter((item) => {
      if (hazardFilter.value === 'matched') return isMatch(item);
      if (hazardFilter.value === 'unmatched') return !isMatch(item);
      if (hazardFilter.value === 'located') return Boolean(latLng(item));
      return true;
    }));
    const shownHazards = computed(() => filteredHazards.value.slice(0, 300));
    const nearest = computed(() => {
      const ranges = detections.value.map((item) => item.distance_m).filter(numeric).map(Number);
      return ranges.length ? Math.min(...ranges) : null;
    });
    const distanceNote = computed(() => {
      if (nearest.value === null) return detections.value.length ? 'Ground projection unavailable' : 'Waiting for a hazard';
      return !verified.value || detections.value.some((item) => item.provisional)
        ? 'Provisional · verify mounting height' : 'Calibrated road-plane estimate';
    });
    const matchCount = computed(() => unlabeled.value && !references.value.length ? '—' : hazards.value.filter(isMatch).length);
    const matchNote = computed(() => unlabeled.value && !references.value.length ? 'Unlabeled · predictions only'
      : `Within ${decimal(dashboard.config?.detection?.match_radius_m, 0)} m · class & time checked`);
    const deviceLabel = computed(() => {
      const device = dashboard.config?.detection?.device;
      if (device === 'auto') return 'Automatic compute device';
      if (!device || device === 'cpu') return 'CPU inference';
      return `GPU ${device} inference`;
    });
    const hasFrame = computed(() => Number(state.value.frame_index) > 0 || Number(state.value.fps) > 0);
    const videoUrl = computed(() => state.value.session_id && (running.value || hasFrame.value)
      ? `/video.mjpg?session=${encodeURIComponent(state.value.session_id)}` : '');
    const frameMeta = computed(() => hasFrame.value ? `${state.value.frame_width} × ${state.value.frame_height} · Frame ${state.value.frame_index}`
      : running.value ? 'Opening source…' : 'No active source');
    const exportUrl = computed(() => state.value.session_id
      ? `/api/export.csv?session_id=${encodeURIComponent(state.value.session_id)}` : '/api/export.csv');
    const poseHelp = computed(() => poseMode.value === 'manual'
      ? 'Fixed vehicle position for stationary testing. Stop detection before changing it.'
      : gpsMessage.value || stopGpsHelp);
    const mapEmpty = computed(() => !posePoint.value && !located.value.length && !routePoints.value.length && !referencePoints.value.length);

    function chooseRecording(id) {
      if (id && selectedId.value !== String(id)) dashboard.selectedRecording = String(id);
    }

    function setSource(type) {
      sourceType.value = type;
      sourcePath.value = type === 'camera' ? '0' : '';
      if (type === 'dataset') poseMode.value = 'dataset';
      else if (poseMode.value === 'dataset') poseMode.value = 'live';
    }

    function openRecording(id) {
      const recording = playableRecordings.value.find((record) => String(record.id) === String(id));
      if (!recording || running.value) return;
      if (dashboard.datasetFilter !== 'all' && dashboard.datasetFilter !== recordingDataset(recording)) {
        dashboard.datasetFilter = recordingDataset(recording);
      }
      setSource('dataset');
      chooseRecording(id);
    }

    function chooseAvailableRecording() {
      const options = visibleRecordings.value;
      if (options.some((recording) => String(recording.id) === selectedId.value)) return;
      const first = options.find((recording) => String(recording.id) === '33') || options[0];
      dashboard.selectedRecording = first ? String(first.id) : '';
    }

    async function loadRecording(id) {
      const version = ++loadVersion;
      if (!id) return;
      try {
        const result = await api(`/api/recordings/${encodeURIComponent(id)}`);
        if (version !== loadVersion || selectedId.value !== id) return;
        dashboard.selectedRecordingDetails = result;
        dashboard.references = result.references || [];
        if (!running.value) await nextTick(fitMap);
      } catch (error) {
        if (version === loadVersion) notify(`Could not load recording: ${error.message}`, true);
      }
    }

    watch(() => dashboard.config, (config) => {
      const pose = config?.pose;
      if (!pose) return;
      for (const field of Object.keys(poseInputs)) poseInputs[field] = pose[field] ?? '';
      if (!running.value) poseMode.value = pose.mode === 'dataset' && sourceType.value !== 'dataset' ? 'live' : pose.mode || 'dataset';
    }, { immediate: true });
    watch(visibleRecordings, chooseAvailableRecording, { immediate: true });
    watch(selectedId, (id) => loadRecording(id), { immediate: true });
    watch(() => dashboard.referencesVersion, () => loadRecording(selectedId.value));
    watch(() => dashboard.openRecordingRequest, (request) => {
      if (request?.id) openRecording(request.id);
    });
    watch(() => state.value.session_id, (sessionId) => {
      disconnected.value = false;
      clearTimeout(modelTimer);
      if (!sessionId || !state.value.source_type) return;
      if (running.value) {
        modelTimer = setTimeout(async () => {
          try {
            if (dashboard.state.session_id !== sessionId) return;
            const { model } = await api('/api/bootstrap');
            if (dashboard.state.session_id === sessionId && model && model.path === dashboard.config?.detection?.model_path) {
              dashboard.model = model;
            }
          } catch (_) { /* Live state also carries the model when it is loaded. */ }
        }, 5000);
      }
      const { source_type: type, pose_mode: mode, recording_id: id } = state.value;
      if (sourceType.value !== type) setSource(type);
      if (mode) poseMode.value = mode;
      if (type === 'dataset' && id) {
        const recording = playableRecordings.value.find((record) => String(record.id) === String(id));
        if (recording && dashboard.datasetFilter !== 'all' && dashboard.datasetFilter !== recordingDataset(recording)) {
          dashboard.datasetFilter = recordingDataset(recording);
        }
        chooseRecording(id);
      }
    }, { immediate: true });
    watch(() => state.value.model, (model) => {
      if (model && model.path === dashboard.config?.detection?.model_path && JSON.stringify(model) !== JSON.stringify(dashboard.model)) {
        clearTimeout(modelTimer);
        dashboard.model = model;
      }
    });

    async function perform(label, action) {
      if (operation.value) return;
      operation.value = label;
      try { await action(); }
      catch (error) { notify(error.message, true); }
      finally { operation.value = ''; }
    }

    function readPose() {
      if (!Object.values(poseInputs).every(numeric)) throw new Error('Enter latitude, longitude, vehicle heading, and GPS accuracy.');
      const pose = Object.fromEntries(Object.entries(poseInputs).map(([key, value]) => [key, Number(value)]));
      if (Math.abs(pose.latitude) > 90 || Math.abs(pose.longitude) > 180 || pose.heading_deg < 0 || pose.heading_deg > 360 || pose.accuracy_m < 0) {
        throw new Error('Check latitude (±90), longitude (±180), heading (0–360°), and GPS accuracy.');
      }
      return pose;
    }

    async function saveManualPose(pose) {
      if (running.value) throw new Error('Stop detection before changing the fixed manual position.');
      const config = JSON.parse(JSON.stringify(dashboard.config));
      config.pose = { ...config.pose, ...pose, mode: 'manual' };
      const response = await api('/api/config', { method: 'POST', body: config });
      dashboard.config = response.config || response;
    }

    function updatePose() {
      return perform('pose', async () => {
        const pose = readPose();
        if (poseMode.value === 'manual') {
          await saveManualPose(pose);
          notify('Manual vehicle position and heading saved.');
        } else {
          await api('/api/pose', { method: 'POST', body: pose });
          notify('GPS fix sent. Keep sending fixes to update the live position.');
        }
      });
    }

    function start() {
      return perform('start', async () => {
        const kind = sourceType.value;
        const mode = poseMode.value;
        if (kind === 'dataset' && !selectedId.value) throw new Error('Choose a recording with a video file.');
        if (kind !== 'dataset' && !sourcePath.value.trim()) throw new Error('Enter the camera index, stream URL, or file path.');
        if (mode === 'manual') await saveManualPose(readPose());
        dashboard.state = await api('/api/session/start', {
          method: 'POST', body: { source_type: kind, recording_id: selectedId.value, source: sourcePath.value.trim(), pose_mode: mode },
        });
        if (mode === 'live' && latestGps) {
          try { await api('/api/pose', { method: 'POST', body: latestGps }); }
          catch (error) { notify(`Detection started. GPS needs a fresh fix: ${error.message}`, true); }
        }
      });
    }

    function stop() {
      return perform('stop', async () => {
        dashboard.state = await api('/api/session/stop', { method: 'POST' });
      });
    }

    async function sendBrowserPose(position) {
      const { latitude, longitude, accuracy, heading } = position.coords;
      const measuredHeading = numeric(heading) ? Number(heading) : null;
      Object.assign(poseInputs, { latitude, longitude, accuracy_m: accuracy });
      if (measuredHeading !== null) poseInputs.heading_deg = measuredHeading.toFixed(1);
      if (!numeric(poseInputs.heading_deg)) {
        gpsMessage.value = 'GPS position received. Enter the measured vehicle heading to forward fixes.';
        return;
      }
      latestGps = { latitude, longitude, heading_deg: measuredHeading ?? Number(poseInputs.heading_deg), accuracy_m: accuracy, timestamp_ms: position.timestamp };
      if (Date.now() - lastGpsSend < 700) return;
      lastGpsSend = Date.now();
      try {
        await api('/api/pose', { method: 'POST', body: latestGps });
        if (gpsWatching.value) gpsMessage.value = `Browser GPS connected · ±${decimal(accuracy)} m · ${measuredHeading === null ? 'manual heading' : 'device travel heading'}.`;
      } catch (error) {
        if (gpsWatching.value) gpsMessage.value = `GPS update rejected: ${error.message}`;
      }
    }

    function stopBrowserGps() {
      if (gpsWatch !== null) navigator.geolocation.clearWatch(gpsWatch);
      gpsWatch = null;
      gpsWatching.value = false;
      latestGps = null;
    }

    function toggleBrowserGps() {
      if (gpsWatching.value) {
        stopBrowserGps();
        gpsMessage.value = 'Browser GPS stopped. Send a fresh fix to continue geolocation.';
        return;
      }
      if (!navigator.geolocation) { notify('Browser geolocation is unavailable.', true); return; }
      gpsWatching.value = true;
      gpsMessage.value = 'Waiting for a browser GPS fix…';
      gpsWatch = navigator.geolocation.watchPosition(sendBrowserPose, (error) => {
        gpsMessage.value = `GPS unavailable: ${error.message}`;
      }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
    }

    watch(poseMode, (mode) => {
      if (mode !== 'live' && gpsWatching.value) stopBrowserGps();
      gpsMessage.value = '';
    });

    function replay() {
      const datasetSource = selectedId.value ? `dataset:${selectedId.value}` : null;
      requestReplay(state.value.session_id ? `session:${state.value.session_id}` : datasetSource, 0, datasetSource);
    }

    function toggleProbe() {
      probing.value = !probing.value;
      probeResult.value = probing.value ? 'Click a visible road point in the camera image.' : '';
    }

    async function probe(event) {
      if (!probing.value || !hasFrame.value) return;
      const image = event.currentTarget;
      const bounds = image.getBoundingClientRect();
      const width = state.value.frame_width || image.naturalWidth;
      const height = state.value.frame_height || image.naturalHeight;
      if (!width || !height) return;
      const scale = Math.min(bounds.width / width, bounds.height / height);
      const x = (event.clientX - bounds.left - (bounds.width - width * scale) / 2) / scale;
      const y = (event.clientY - bounds.top - (bounds.height - height * scale) / 2) / scale;
      if (x < 0 || y < 0 || x >= width || y >= height) return;
      const u = Math.min(width - 1, x), v = Math.min(height - 1, y);
      try {
        const result = await api('/api/calibration/probe', { method: 'POST', body: { u, v, frame_width: width, frame_height: height } });
        const projection = result.projection || result;
        const pixel = `Pixel (${Math.round(u)}, ${Math.round(v)})`;
        if (!projection.valid) {
          probeResult.value = `${pixel}: ${projection.reason || 'No valid ground intersection'}`;
          return;
        }
        const position = latLng(result);
        const location = position ? `GPS ${decimal(position[0], 6)}, ${decimal(position[1], 6)}` : result.location?.reason || 'GPS unavailable';
        probeResult.value = `${pixel}: ${decimal(projection.distance_m)} m range · ${decimal(projection.forward_m)} m forward · ${decimal(projection.right_m)} m right. ${location}${projection.provisional ? ' · Provisional calibration' : ''}`;
      } catch (error) { probeResult.value = error.message; }
    }

    function makePopup(item, point, reference = false) {
      const popup = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = reference ? `Reference · ${hazardName(item)}` : hazardName(item);
      popup.append(title);
      const lines = reference
        ? [`Record ${item.id || '—'}`, item.location_kind === 'vehicle_at_annotation' ? 'Vehicle position at annotation' : 'Imported reference']
        : [`${confidence(item)}% confidence · ${item.observations || 1} observations`,
          numeric(item.distance_m) ? `${decimal(item.distance_m)} m ground range` : 'Ground range unavailable',
          item.provisional ? 'Provisional calibration' : 'Calibration marked verified'];
      if (!reference && item.class_ambiguous) lines.push('Class varied between frames.');
      if (!reference && numeric(item.gps_accuracy_m)) lines.push(`Reported GPS accuracy ±${decimal(item.gps_accuracy_m)} m.`);
      if (!reference && isMatch(item)) lines.push(`Reference ${item.match.reference_id || '—'}`);
      lines.push(point.map((value) => decimal(value, 6)).join(', '));
      for (const text of lines) {
        const row = document.createElement('div');
        row.textContent = text;
        popup.append(row);
      }
      return popup;
    }

    function updateRoute() {
      if (!map) return;
      routeLayer.setLatLngs(routePoints.value);
      referenceLayer.clearLayers();
      for (const { item, point } of referencePoints.value) {
        window.L.circleMarker(point, { radius: 5, color: '#d8ad69', fillColor: '#eab96b', weight: 1.5, fillOpacity: .28 })
          .bindPopup(makePopup(item, point, true)).addTo(referenceLayer);
      }
    }

    function updateMarkers() {
      if (!map) return;
      const visible = new Set();
      for (const { item, point } of located.value) {
        const key = String(item.id);
        visible.add(key);
        let marker = markers.get(key);
        const signature = JSON.stringify([point, item.class_name, item.confidence, item.observations, item.distance_m, item.provisional, item.match]);
        if (!marker) {
          const icon = window.L.divIcon({ className: 'hazard-marker',
            html: `<div class="hazard-pin${item.provisional ? ' provisional' : ''}"></div>`, iconSize: [17, 17], iconAnchor: [8, 8] });
          marker = window.L.marker(point, { icon }).bindPopup(makePopup(item, point)).addTo(hazardLayer);
          markers.set(key, marker);
        } else if (marker.roadwatchSignature !== signature) {
          marker.setLatLng(point).setPopupContent(makePopup(item, point));
        }
        marker.roadwatchSignature = signature;
      }
      for (const [key, marker] of markers) {
        if (!visible.has(key)) { hazardLayer.removeLayer(marker); markers.delete(key); }
      }
      const point = posePoint.value;
      if (!point) {
        if (vehicle) { map.removeLayer(vehicle); vehicle = null; }
        return;
      }
      if (!vehicle) {
        const icon = window.L.divIcon({ className: 'vehicle-icon', html: '<div class="vehicle-pin">↑</div>', iconSize: [25, 25], iconAnchor: [12, 12] });
        vehicle = window.L.marker(point, { icon, zIndexOffset: 1000 }).bindTooltip('Camera vehicle').addTo(map);
      } else vehicle.setLatLng(point);
      const pin = vehicle.getElement()?.querySelector('.vehicle-pin');
      if (pin) {
        const heading = state.value.pose?.heading_deg;
        pin.textContent = numeric(heading) ? '↑' : '•';
        pin.style.transform = `rotate(${numeric(heading) ? Number(heading) : 0}deg)`;
      }
      if (follow.value && running.value && dashboard.tab === 'live') map.setView(point, Math.max(map.getZoom(), 17), { animate: false });
    }

    function fitMap() {
      if (!map || dashboard.tab !== 'live') return;
      const points = [...sample(routePoints.value, 1000), ...referencePoints.value.map(({ point }) => point),
        ...located.value.map(({ point }) => point)];
      if (posePoint.value) points.push(posePoint.value);
      if (points.length) map.fitBounds(window.L.latLngBounds(points), { padding: [30, 30], maxZoom: 18 });
    }

    function focusHazard(item) {
      const point = latLng(item);
      if (!point || !map) return;
      follow.value = false;
      map.setView(point, 19);
      markers.get(String(item.id))?.openPopup();
      mapElement.value?.scrollIntoView({ block: 'center' });
    }

    function makeMap() {
      if (map) return;
      if (!window.L) {
        mapOffline.value = 'The local map library could not load. GPS coordinates remain available in the hazard table.';
        return;
      }
      const L = window.L;
      map = L.map(mapElement.value, { zoomControl: true, preferCanvas: true, minZoom: 2, maxZoom: 20 }).setView([20, 0], 3);
      const offline = 'Map tiles unavailable. GPS markers and route remain active.';
      const tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
      }).addTo(map);
      tiles.on('tileerror', () => { mapOffline.value = offline; });
      tiles.on('tileload', () => { receivedTiles = true; mapOffline.value = ''; });
      tileTimer = setTimeout(() => { if (!receivedTiles) mapOffline.value = offline; }, 12000);
      routeLayer = L.polyline([], { color: '#649ec6', weight: 3, opacity: .65 }).addTo(map);
      referenceLayer = L.layerGroup().addTo(map);
      hazardLayer = L.layerGroup().addTo(map);
      L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);
      updateRoute();
      updateMarkers();
      fitMap();
    }

    watch([routePoints, referencePoints], updateRoute);
    watch([located, posePoint, follow], () => {
      if (!map || mapTimer) return;
      mapTimer = setTimeout(() => { mapTimer = null; updateMarkers(); }, 250);
    });
    watch(() => dashboard.tab, async (tab) => {
      if (tab !== 'live') return;
      await nextTick();
      const existing = Boolean(map);
      makeMap();
      if (existing && map) { map.invalidateSize(); updateMarkers(); }
    });
    onMounted(() => { if (dashboard.tab === 'live') makeMap(); });
    onBeforeUnmount(() => {
      stopBrowserGps();
      clearTimeout(tileTimer);
      clearTimeout(mapTimer);
      clearTimeout(modelTimer);
      map?.remove();
    });

    return {
      dashboard, state, sourceType, sourcePath, poseMode, poseInputs, operation, gpsWatching, poseHelp, hazardFilter, follow,
      probing, probeResult, disconnected, mapElement, mapOffline, mapEmpty, sourceFields, statusText, videoStatus, running,
      selectedId, visibleRecordings, selected, comma, unlabeled, verified, located, posePoint, hazards, detections, shownHazards,
      filteredHazards, nearest, distanceNote, matchCount, matchNote, deviceLabel, videoUrl, hasFrame, frameMeta, exportUrl,
      numeric, decimal, clock, latLng, hazardName, isMatch, confidence, datasetName, recordingDataset, go, setSource, chooseRecording,
      updatePose, start, stop, toggleBrowserGps, replay, toggleProbe, probe, fitMap, focusHazard,
    };
  },
  template: `
  <section id="tab-live" class="tab-panel" aria-labelledby="live-title">
    <div class="page-heading">
      <div><h1 id="live-title">Live hazard monitor</h1><p>Detect road hazards, estimate their distance, and see their positions on the map.</p></div>
      <a class="button secondary export-button" :href="exportUrl">
        <svg viewBox="0 0 24 24"><path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/></svg>Export detections
      </a>
    </div>

    <div id="calibration-banner" class="notice" :class="verified ? 'verified' : 'amber'">
      <span class="notice-icon">{{ verified ? '✓' : '!' }}</span>
      <div>
        <strong>{{ comma ? 'comma2k19 camera intrinsics' : verified ? 'Measured calibration acknowledged' : 'Calibration needs verification' }}</strong>
        <span>{{ comma ? 'Published intrinsics and available camera orientation apply automatically. Distance and hazard GPS remain provisional.' : verified ? 'Road geometry and GPS accuracy still affect estimates.' : 'Use measured camera settings to improve distance and GPS estimates.' }}</span>
      </div>
      <button type="button" class="text-button" @click="go('calibration')">Configure camera <span>↗</span></button>
    </div>

    <form id="session-form" class="source-bar" @submit.prevent="start">
      <label class="field source-kind"><span>INPUT SOURCE</span>
        <select id="source-type" :value="sourceType" :disabled="running" @change="setSource($event.target.value)">
          <option value="dataset">Dataset recording</option><option value="camera">Live camera</option>
          <option value="stream">IP / RTSP stream</option><option value="file">Video file path</option>
        </select>
      </label>
      <label v-show="sourceType === 'dataset'" class="field dataset-selection"><span>DATASET</span>
        <select id="dataset-select" v-model="dashboard.datasetFilter" :disabled="running">
          <option value="all">All datasets</option><option value="roadsens">RoadSens-4M</option><option value="comma2k19">comma2k19 · Unlabeled</option>
        </select>
      </label>
      <label v-show="sourceType === 'dataset'" class="field source-selection"><span>RECORDING</span>
        <select id="recording-select" :value="selectedId" :disabled="running" @change="chooseRecording($event.target.value)">
          <option v-if="!visibleRecordings.length" value="">{{ dashboard.ready ? 'No playable recordings' : 'Loading recordings…' }}</option>
          <option v-for="record in visibleRecordings" :key="record.id" :value="String(record.id)">
            {{ record.label || 'Recording ' + record.id }}{{ recordingDataset(record) === 'comma2k19' ? ' · Unlabeled' : '' }}
          </option>
        </select>
      </label>
      <label v-if="sourceType !== 'dataset'" class="field source-selection">
        <span>{{ sourceFields[sourceType]?.[0] }}</span>
        <input id="source-path" v-model="sourcePath" :placeholder="sourceFields[sourceType]?.[1]" :disabled="running">
      </label>
      <label class="field pose-selection"><span>GPS SOURCE</span>
        <select id="pose-mode" v-model="poseMode" :disabled="running">
          <option value="dataset" :disabled="sourceType !== 'dataset'">Dataset track</option>
          <option value="manual">Manual position</option><option value="live">Live GPS</option>
        </select>
      </label>
      <div class="session-actions">
        <button id="start-button" type="submit" class="button primary" :disabled="running || !dashboard.ready || !!operation">
          <svg viewBox="0 0 24 24"><path d="m9 5 11 7-11 7z"/></svg>{{ operation === 'start' ? 'Starting…' : 'Start detection' }}
        </button>
        <button id="stop-button" type="button" class="button danger" :disabled="!running || !!operation" @click="stop">
          <svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>{{ operation === 'stop' ? 'Stopping…' : 'Stop' }}
        </button>
      </div>
    </form>

    <div class="live-replay-actions"><button type="button" id="replay-current" class="text-button" :disabled="!state.session_id && !selectedId" @click="replay">Replay with detection ↗</button><span>Up to 10 evidence frames are saved per tracked hazard.</span></div>
    <div v-if="selected" id="recording-notice" class="recording-notice" role="status">
      <span id="recording-dataset" class="badge" :class="unlabeled ? 'provisional' : 'neutral'">{{ datasetName(recordingDataset(selected)) }}</span>
      <span>{{ unlabeled ? 'Unlabeled · detections are predictions' : 'Reference annotations may use vehicle position at the event time.' }}</span>
    </div>

    <div v-show="poseMode !== 'dataset'" class="panel compact-panel">
      <form id="pose-form" class="inline-form" @submit.prevent="updatePose">
        <label class="field"><span>LATITUDE</span><input id="pose-latitude" v-model="poseInputs.latitude" type="number" step="any" min="-90" max="90" placeholder="e.g. 40.7128" required></label>
        <label class="field"><span>LONGITUDE</span><input id="pose-longitude" v-model="poseInputs.longitude" type="number" step="any" min="-180" max="180" placeholder="e.g. -74.0060" required></label>
        <label class="field"><span>VEHICLE HEADING · ° FROM NORTH</span><input id="pose-heading" v-model="poseInputs.heading_deg" type="number" step="any" min="0" max="360" placeholder="Required for hazard geolocation" required></label>
        <label class="field"><span>GPS ACCURACY · M</span><input id="pose-accuracy" v-model="poseInputs.accuracy_m" type="number" min="0" step="any" required></label>
        <button type="submit" class="button secondary" :disabled="!!operation || !dashboard.ready">{{ operation === 'pose' ? 'Saving…' : poseMode === 'manual' ? 'Save manual pose' : 'Send GPS fix' }}</button>
        <button v-if="poseMode === 'live'" id="browser-gps-button" type="button" class="button secondary" @click="toggleBrowserGps">{{ gpsWatching ? 'Stop browser GPS' : 'Use browser GPS' }}</button>
      </form>
      <p id="pose-help" class="helper">{{ poseHelp }}</p>
    </div>

    <div class="metrics-grid">
      <article class="metric-card">
        <span class="metric-icon teal"><svg viewBox="0 0 24 24"><path d="m12 3 10 18H2zM12 9v5m0 3v1"/></svg></span>
        <div><span class="metric-label">UNIQUE HAZARDS</span><div class="metric-value" id="metric-hazards">{{ hazards.length }}</div></div>
        <span class="metric-note">{{ state.total_detections || 0 }} observations</span>
      </article>
      <article class="metric-card">
        <span class="metric-icon amber"><svg viewBox="0 0 24 24"><path d="M3 17 17 3l4 4L7 21zM7 13l3 3m1-7 3 3m1-7 3 3"/></svg></span>
        <div><span class="metric-label">NEAREST IN FRAME</span><div class="metric-value">{{ decimal(nearest) }}<small> m</small></div></div>
        <span class="metric-note">{{ distanceNote }}</span>
      </article>
      <article class="metric-card">
        <span class="metric-icon blue"><svg viewBox="0 0 24 24"><path d="M5 4h14v17H5zM8 2v4m8-4v4m-8 6 3 3 5-6"/></svg></span>
        <div><span class="metric-label">RECORD MATCHES</span><div class="metric-value">{{ matchCount }}</div></div>
        <span class="metric-note">{{ matchNote }}</span>
      </article>
      <article class="metric-card">
        <span class="metric-icon violet"><svg viewBox="0 0 24 24"><path d="m13 2-9 12h7l-1 8 10-13h-8z"/></svg></span>
        <div><span class="metric-label">PROCESSING SPEED</span><div class="metric-value">{{ state.fps > 0 ? decimal(state.fps) : '—' }}<small> fps</small></div></div>
        <span class="metric-note">{{ deviceLabel }}</span>
      </article>
    </div>

    <div class="monitor-grid">
      <article class="panel video-panel">
        <div class="panel-heading">
          <div class="panel-title"><span id="video-status" class="live-pill" :class="{ active: running, error: state.status === 'error' }">{{ videoStatus[state.status] || 'IDLE' }}</span><h2>Camera feed</h2></div>
          <span id="frame-meta" class="muted small">{{ frameMeta }}</span>
        </div>
        <div id="video-stage" class="video-stage">
          <img v-if="videoUrl" v-show="hasFrame" id="live-frame" :src="videoUrl" :class="{ probing }" alt="Live camera feed with hazard detections and distances" @click="probe" @error="disconnected = true" @load="disconnected = false">
          <div v-if="!hasFrame" class="video-empty">
            <div class="empty-camera"><svg viewBox="0 0 48 48"><rect x="7" y="13" width="26" height="24" rx="5"/><path d="m33 21 10-6v20l-10-6M16 7h9M17 24h7m-3.5-3.5v7"/></svg></div>
            <h3>Select a recording or connect a camera</h3><p>Start detection to inspect the road.</p>
          </div>
          <span class="video-corner top-left" aria-hidden="true"></span><span class="video-corner bottom-right" aria-hidden="true"></span>
          <div v-if="hasFrame" class="video-overlay">
            <span>{{ clock(state.video_time_s) }} · {{ detections.length }} in frame</span>
            <span>{{ posePoint ? (numeric(state.pose?.heading_deg) ? 'GPS · ' + decimal(state.pose.heading_deg, 0) + '°' : 'GPS · heading unavailable') : 'GPS unavailable' }}</span>
          </div>
        </div>
        <div class="video-footer"><span><i class="legend-box"></i> Hazard bounding box</span>
          <button id="probe-toggle" class="text-button" type="button" :disabled="!hasFrame" @click="toggleProbe">{{ probing ? 'Exit point inspector ×' : 'Inspect a road point ⌖' }}</button>
        </div>
        <div v-if="probing" id="probe-result" class="probe-result" role="status">{{ probeResult }}</div>
      </article>
      <article class="panel map-panel">
        <div class="panel-heading">
          <div class="panel-title"><h2>Hazard map</h2><span id="map-hazard-count" class="count-chip">{{ located.length }}</span></div>
          <label class="toggle-label"><input id="map-follow" v-model="follow" type="checkbox"><span>Follow GPS</span></label>
        </div>
        <div class="map-wrap">
          <div id="hazard-map" ref="mapElement" aria-label="Map of vehicle route, detected hazards and reference records"></div>
          <div v-if="mapEmpty" class="map-empty">
            <svg viewBox="0 0 24 24"><path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2zM9 3v16m6-14v16"/></svg>
            <strong>Waiting for a GPS position</strong><span>Hazard markers require position and vehicle heading.</span>
          </div>
          <div v-if="mapOffline" class="map-offline">{{ mapOffline }}</div>
          <button id="map-fit" class="map-fit" type="button" aria-label="Fit route and hazards" title="Fit route and hazards" @click="follow = false; fitMap()">⌖</button>
        </div>
        <div class="map-footer"><span><i class="legend-dot teal-dot"></i>Detected hazard</span><span><i class="legend-dot amber-dot"></i>Reference record</span><span><i class="legend-line"></i>GPS track</span></div>
      </article>
    </div>

    <div class="status-strip">
      <span class="status-item"><span class="status-dot" :class="{ running, error: state.status === 'error' }"></span><strong>{{ statusText[state.status] || 'Ready' }}</strong></span>
      <span id="session-message">{{ disconnected && running ? 'Camera image stream disconnected. Waiting for a frame.' : state.message || 'Choose a source to begin.' }}</span>
      <span class="status-end">{{ posePoint ? 'GPS ' + posePoint.map((value) => decimal(value, 5)).join(', ') + ' · ' + (numeric(state.pose.accuracy_m) ? '±' + decimal(state.pose.accuracy_m) + ' m' : 'accuracy unknown') : 'GPS — waiting for position & heading' }}</span>
    </div>
    <article class="panel detections-panel">
      <div class="panel-heading">
        <div class="panel-title"><h2>Detected hazards</h2><span id="table-hazard-count" class="count-chip">{{ hazards.length }}</span></div>
        <div class="table-actions"><label class="sr-only" for="hazard-filter">Filter hazards</label>
          <select id="hazard-filter" v-model="hazardFilter" class="small-select">
            <option value="all">All hazards</option><option value="matched">Matched records</option><option value="unmatched">No record match</option><option value="located">With GPS position</option>
          </select>
        </div>
      </div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>HAZARD / ID</th><th>CONFIDENCE</th><th>DISTANCE</th><th>GPS COORDINATES</th><th>RECORD MATCH</th><th>OBSERVATIONS</th><th>LAST SEEN</th><th>REPORTING</th></tr></thead>
          <tbody id="hazards-body">
            <tr v-for="item in shownHazards" :key="item.id" :tabindex="latLng(item) ? 0 : undefined"
                :title="latLng(item) ? 'Show this hazard on the map' : undefined" :style="{ cursor: latLng(item) ? 'pointer' : undefined }"
                @click="focusHazard(item)" @keydown.enter.self="focusHazard(item)">
              <td><div class="hazard-name"><i>⌁</i><div><strong>{{ hazardName(item) }}</strong><small>#{{ String(item.id || '—').slice(0, 16) }}</small><small v-if="item.class_ambiguous">Class varied across frames</small></div></div></td>
              <td><span class="confidence-bar" :style="{ '--confidence': confidence(item) + '%' }">{{ confidence(item) }}%</span></td>
              <td><strong>{{ numeric(item.distance_m) ? decimal(item.distance_m) + ' m' : 'Unavailable' }}</strong><small v-if="item.provisional">Provisional estimate</small></td>
              <td class="coords" :title="latLng(item) ? (numeric(item.gps_accuracy_m) ? 'Reported GPS accuracy ±' + decimal(item.gps_accuracy_m) + ' m' : 'GPS accuracy unknown') : item.projection_reason || 'A valid GPS position and heading are required.'">
                <template v-if="latLng(item)"><div>{{ decimal(item.latitude, 6) }}</div><div>{{ decimal(item.longitude, 6) }}</div></template>
                <template v-else>GPS unavailable</template>
              </td>
              <td>
                <template v-if="isMatch(item)"><span class="badge matched">{{ item.match.match_kind === 'temporal' ? 'Time candidate' : 'Matched candidate' }}</span>
                  <small>{{ numeric(item.match.distance_m) ? decimal(item.match.distance_m) + ' m · ' + String(item.match.reference_id || '').slice(0, 10) : 'Δ ' + decimal(item.match.time_delta_s) + ' s' }}</small></template>
                <span v-else class="badge neutral">{{ matchCount === '—' ? 'Unlabeled' : 'No match' }}</span>
              </td>
              <td>{{ item.observations || 1 }}</td><td>{{ clock(item.last_seen_s) }}</td>
              <td><hazard-report-actions :hazard-id="item.id"></hazard-report-actions></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!shownHazards.length" class="table-empty">
          <svg viewBox="0 0 24 24"><path d="M4 7h16v14H4zM8 7V3h8v4m-8 6h8"/></svg>
          <strong>{{ hazards.length ? 'No hazards match this filter' : 'No hazards detected yet' }}</strong>
        </div>
      </div>
      <div class="panel-bottom">
        <span>{{ hazards.length }} {{ hazards.length === 1 ? 'hazard' : 'hazards' }} in this session{{ hazardFilter !== 'all' ? ' · ' + filteredHazards.length + ' matching filter' : '' }}{{ filteredHazards.length > 300 ? ' · Showing latest 300' : '' }}</span>
        <span>Distance estimates use camera geometry</span>
      </div>
    </article>
  </section>`,
};
