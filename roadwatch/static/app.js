/* RoadWatch dashboard. The server is the source of truth for all inference data. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const app = { config: null, state: {}, recordings: [], references: [], track: [], map: null,
    socket: null, pollTimer: null, reconnectTimer: null, toastTimer: null, currentTab: 'live',
    referenceLayer: null, hazardLayer: null, routeLayer: null, vehicle: null, markers: new Map(),
    selectedRecording: null, loadedRecording: null, loadingRecording: 0, session: null,
    videoSession: null, probe: false, geoWatch: null, geoPose: null, lastGeoSend: 0, pending: 0,
    lastRender: 0, bootstrapped: false, tileSuccesses: 0, busy: false, lastHazardSignature: '', lastMapSignature: '',
    datasets: [], datasetSummary: {}, datasetFilter: 'all', recordingInfo: null, recordingCalibration: null,
    calibrationMetadata: null, download: null, datasetTimer: null, datasetPollBusy: false, datasetsRefreshing: false,
    lastDownloadFiles: null, pendingDatasetRefresh: false };
  const numeric = (value) => value !== null && value !== '' && value !== undefined && Number.isFinite(Number(value));
  const valueOr = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : String(value);
  const display = (value, digits = 1) => numeric(value) ? Number(value).toFixed(digits) : '—';
  const time = (value) => {
    if (!numeric(value)) return '—';
    const seconds = Math.max(0, Math.floor(Number(value)));
    return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
  };
  const latlng = (item) => {
    if (!item) return null;
    const lat = item.latitude ?? item.lat ?? item.location_latitude;
    const lon = item.longitude ?? item.lon ?? item.lng ?? item.location_longitude;
    return numeric(lat) && numeric(lon) && Math.abs(Number(lat)) <= 90 && Math.abs(Number(lon)) <= 180 ? [Number(lat), Number(lon)] : null;
  };
  const titleCase = (name) => String(name ?? 'Unknown').replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  const className = (item) => titleCase(item.class_name ?? item.hazard_type ?? item.type);
  const isRunning = () => app.state.status === 'running';
  const matched = (item) => Boolean(item.match && item.match.matched !== false && (item.match.reference_id || item.match.reference || item.match.match_kind));
  const recordingDataset = (recording) => recording?.dataset_id || 'roadsens';
  const datasetName = (id) => app.datasets.find((dataset) => dataset.id === id)?.name || ({ roadsens: 'RoadSens-4M', comma2k19: 'comma2k19' }[id] ?? id);
  const selectedDatasetRecording = () => $('source-type').value === 'dataset' ? app.recordingInfo : null;
  const unlabeledRecording = () => Boolean(selectedDatasetRecording() && (app.recordingInfo.has_labels === false || recordingDataset(app.recordingInfo) === 'comma2k19'));
  const formatBytes = (value) => {
    if (!numeric(value)) return 'Unknown size';
    const bytes = Math.max(0, Number(value));
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    const unit = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / (1024 ** unit)).toFixed(unit > 1 ? 2 : 1)} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit]}`;
  };
  const element = (tag, text, className = '') => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function toast(message, error = false) {
    clearTimeout(app.toastTimer);
    $('toast').textContent = message;
    $('toast').classList.toggle('error', error);
    $('toast').hidden = false;
    app.toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 12000 : 6500);
  }
  function connection(label, online = true) {
    const node = $('connection-status');
    node.replaceChildren(element('span'), document.createTextNode(label));
    node.classList.toggle('online', online);
    node.classList.toggle('offline', !online);
  }
  async function api(url, options = {}) {
    const request = { ...options, headers: { ...options.headers } };
    if (request.body && !(request.body instanceof FormData)) {
      request.headers['Content-Type'] = 'application/json';
      request.body = JSON.stringify(request.body);
    }
    const response = await fetch(url, request);
    let payload;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (!response.ok) {
      const detail = payload?.detail;
      throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((entry) => entry.msg || JSON.stringify(entry)).join('; ') : `Request failed (${response.status}).`);
    }
    return payload;
  }
  async function busy(button, task, label) {
    const old = button.textContent;
    app.pending += 1; app.busy = true;
    button.disabled = true;
    updateControls();
    if (label) button.textContent = label;
    try { return await task(); }
    catch (error) { toast(error.message || 'The operation could not be completed.', true); return null; }
    finally { button.textContent = old; button.disabled = false; app.pending -= 1; app.busy = app.pending > 0; updateControls(); }
  }
  function setTab(name) {
    if (!$(`tab-${name}`)) name = 'live';
    app.currentTab = name;
    $$('.tab-panel').forEach((panel) => { panel.hidden = panel.id !== `tab-${name}`; panel.classList.toggle('active', !panel.hidden); });
    $$('.nav-item').forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle('active', active);
      if (active) { button.setAttribute('aria-current', 'page'); $('breadcrumb-current').textContent = button.textContent.trim(); }
      else button.removeAttribute('aria-current');
    });
    if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    if (name === 'live') setTimeout(() => app.map?.invalidateSize(), 50);
    window.dispatchEvent(new CustomEvent('roadwatch:tab', { detail: { name } }));
  }
  function getNested(obj, path) { return path.split('.').reduce((value, key) => value?.[key], obj); }
  function setNested(obj, path, value) {
    const keys = path.split('.');
    let current = obj;
    keys.slice(0, -1).forEach((key) => { current[key] ??= {}; current = current[key]; });
    current[keys.at(-1)] = value;
  }
  function populateConfig() {
    $$('[data-config]').forEach((input) => {
      const value = getNested(app.config, input.dataset.config);
      if (input.type === 'checkbox') input.checked = Boolean(value);
      else input.value = value ?? '';
    });
    $('distortion-input').value = (app.config.calibration.distortion || []).join(', ');
    $('pose-mode').value = app.config.pose.mode || 'dataset';
    for (const key of ['latitude', 'longitude', 'heading_deg', 'accuracy_m']) {
      const id = { latitude: 'pose-latitude', longitude: 'pose-longitude', heading_deg: 'pose-heading', accuracy_m: 'pose-accuracy' }[key];
      $(id).value = app.config.pose[key] ?? '';
    }
    updateCalibrationBanner();
    updateSourceFields();
  }
  function configFromForm() {
    const config = structuredClone(app.config);
    $$('[data-config]').forEach((input) => {
      const value = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value.trim();
      setNested(config, input.dataset.config, value);
    });
    const pieces = $('distortion-input').value.split(/[\s,]+/).filter(Boolean);
    const distortion = pieces.map(Number);
    if (distortion.some((value) => !Number.isFinite(value)) || ![4, 5, 8, 12, 14].includes(distortion.length)) throw new Error('Enter 4, 5, 8, 12, or 14 numeric OpenCV distortion coefficients.');
    config.calibration.distortion = distortion;
    return config;
  }
  function updateCalibrationBanner() {
    const datasetCalibration = selectedDatasetRecording() && recordingDataset(app.recordingInfo) === 'comma2k19';
    const verified = !datasetCalibration && Boolean(app.config?.calibration?.verified);
    const banner = $('calibration-banner');
    banner.classList.toggle('verified', verified);
    banner.classList.toggle('amber', !verified);
    banner.querySelector('.notice-icon').textContent = verified ? '✓' : '!';
    banner.querySelector('strong').textContent = datasetCalibration ? 'comma2k19 camera intrinsics · height needs measurement' : verified ? 'Measured calibration acknowledged' : 'Calibration needs verification';
    const calibration = app.recordingCalibration;
    const cameraDetail = datasetCalibration && calibration ? `Camera model: ${calibration.width} × ${calibration.height} px, fx ${display(calibration.fx, 1)}, fy ${display(calibration.fy, 1)}. ` : '';
    const orientationDetail = app.recordingInfo?.camera_pose_available === false ? 'Synchronized camera orientation is unavailable for this segment.' : 'Published intrinsics and synchronized camera orientation apply automatically.';
    banner.querySelector(':scope > div > span').textContent = datasetCalibration ? `${cameraDetail}${orientationDetail} Height above the road remains an assumption; distance and hazard GPS remain provisional.` : verified ? 'Camera calibration is marked verified. Road geometry and GPS accuracy still affect estimated distance and location.' : 'Default camera values are estimates. Distance and GPS positions remain provisional until you enter measured calibration.';
    $('dataset-calibration-notice').hidden = !datasetCalibration;
    if (datasetCalibration) $('dataset-calibration-detail').textContent = `${cameraDetail}${calibration ? `Principal point: (${display(calibration.cx, 1)}, ${display(calibration.cy, 1)}) px. Assumed height: ${display(calibration.height_m, 2)} m. ` : ''}${orientationDetail} Change Height above road below to adjust the distance scale. The saved lens values and mounting angles continue to configure your own camera and RoadSens recordings. Published intrinsics do not measure camera height; distance and hazard GPS remain provisional.`;
    $('metric-match-note').textContent = unlabeledRecording() && !app.references.length ? 'Unlabeled · predictions only' : `Within ${display(app.config?.detection?.match_radius_m, 0)} m · class & time checked`;
    const device = app.config?.detection?.device;
    $('metric-device').textContent = device === 'auto' ? 'Automatic compute device' : device === 'cpu' ? 'CPU inference' : `GPU ${device} inference`;
  }
  function renderModel(model = {}) {
    const path = model.path || app.config?.detection?.model_path || 'best.pt';
    $('model-name').textContent = String(path).split(/[\\/]/).at(-1);
    $('model-name').title = path;
    const classes = Array.isArray(model.classes) ? model.classes : Object.values(model.classes || {});
    $('model-class-count').textContent = classes.length ? classes.join(' · ') : 'Trained classes load on start';
    $('model-class-count').title = classes.join(', ');
    $('model-status').textContent = model.status || 'Local YOLO model';
  }
  function updateSourceFields() {
    const type = $('source-type').value;
    $('recording-field').hidden = type !== 'dataset';
    $('dataset-field').hidden = type !== 'dataset';
    $('source-path-field').hidden = type === 'dataset';
    const labels = { camera: ['CAMERA INDEX', '0'], stream: ['STREAM URL', 'rtsp://camera-address/stream'], file: ['LOCAL VIDEO PATH', 'E:\\path\\to\\recording.mp4'] };
    if (labels[type]) {
      $('source-path-label').textContent = labels[type][0];
      $('source-path').placeholder = labels[type][1];
    }
    const datasetOption = $('pose-mode').querySelector('[value="dataset"]');
    datasetOption.disabled = type !== 'dataset';
    if (type !== 'dataset' && $('pose-mode').value === 'dataset') $('pose-mode').value = 'live';
    const poseMode = $('pose-mode').value;
    $('pose-controls').hidden = poseMode === 'dataset';
    $('browser-gps-button').hidden = poseMode !== 'live';
    $('pose-help').textContent = poseMode === 'manual' ? 'Fixed vehicle position for stationary testing. Save latitude, longitude and heading before starting. Stop detection before changing it.' : 'Connect a GPS bridge to POST /api/pose, or use browser GPS. Enter a vehicle heading if the device cannot report one. A phone or laptop GPS must be at the vehicle antenna position.';
    const manual = poseMode === 'manual';
    $('pose-form').querySelector('[type="submit"]').textContent = manual ? 'Save manual pose' : 'Send GPS fix';
    $('pose-latitude').required = manual;
    $('pose-longitude').required = manual;
    $('pose-heading').required = manual;
    if (type === 'dataset' && app.loadedRecording) renderTrack();
    else if (type !== 'dataset') app.routeLayer?.setLatLngs([]);
    renderReferenceMap();
    renderRecordingNotice();
    updateCalibrationBanner();
  }
  function updateControls() {
    const running = isRunning();
    $('start-button').disabled = running || !app.bootstrapped || app.busy;
    $('stop-button').disabled = !running || app.busy;
    ['source-type', 'recording-select', 'source-path', 'pose-mode', 'dataset-select', 'records-dataset-select'].forEach((id) => { $(id).disabled = running; });
    $('refresh-datasets').disabled = running || app.datasetsRefreshing || app.busy;
  }
  function renderRecordings() {
    const selector = $('recording-select');
    const previous = selector.value || app.selectedRecording || app.state.recording_id;
    const recordings = app.recordings.filter((recording) => app.datasetFilter === 'all' || recordingDataset(recording) === app.datasetFilter);
    selector.replaceChildren();
    recordings.filter((recording) => recording.video_path).forEach((recording) => {
      const option = element('option', `${recording.label || `Recording ${recording.id}`}${recordingDataset(recording) === 'comma2k19' ? ' · Unlabeled' : ''}`);
      option.value = recording.id;
      selector.append(option);
    });
    if (!selector.options.length) { selector.append(element('option', 'No playable recordings')); selector.options[0].value = ''; }
    if ([...selector.options].some((option) => option.value === '33')) selector.value = '33';
    if (previous && [...selector.options].some((option) => option.value === String(previous))) selector.value = String(previous);
    $('recordings-count').textContent = recordings.length;
    const list = $('recordings-list');
    list.replaceChildren();
    if (!recordings.length) list.append(element('div', app.datasetFilter === 'comma2k19' ? 'No complete comma2k19 segments are available yet. Download progress appears above.' : 'No recordings found for this dataset.', 'table-empty'));
    recordings.forEach((recording) => {
      const row = element('div', null, 'recording-row');
      const icon = element('span', '▷', 'recording-icon');
      const info = element('div');
      const unlabeled = recording.has_labels === false || recordingDataset(recording) === 'comma2k19';
      info.append(element('strong', recording.label || `Recording ${recording.id}`), element('small', [datasetName(recordingDataset(recording)), recording.video_path ? 'Video available' : 'Video missing', recording.sensor_path || recording.has_gps ? 'GPS track' : 'No GPS track', unlabeled ? 'Unlabeled · predictions only' : recording.annotation_path ? 'Anomaly annotations' : 'Labels unavailable'].join(' · ')));
      if (unlabeled) info.append(element('span', 'Unlabeled', 'badge provisional recording-badge'));
      const button = element('button', recording.video_path ? 'Open ↗' : 'No video', 'button secondary');
      button.type = 'button';
      button.disabled = !recording.video_path;
      button.addEventListener('click', () => {
        if (isRunning()) { toast('Stop the active session before choosing another recording.', true); return; }
        selector.value = String(recording.id);
        $('source-type').value = 'dataset';
        $('pose-mode').value = 'dataset';
        updateSourceFields();
        loadRecording(selector.value);
        setTab('live');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      });
      row.append(icon, info, button);
      list.append(row);
    });
  }
  function renderDataset(dataset = {}) {
    app.datasetSummary = dataset;
    const holder = $('dataset-summary');
    holder.replaceChildren();
    const stats = [['Playable recordings', dataset.playable_recording_count ?? app.recordings.filter((r) => r.video_path).length, `of ${dataset.recording_count ?? app.recordings.length} folders`], ['Recorded anomaly events', dataset.reference_event_count ?? '—', 'Annotation.csv'], ['Excel workbooks found', dataset.workbook_count ?? 0, dataset.workbook_count ? 'in dataset' : 'Upload to add yours']];
    stats.forEach(([label, value, note]) => {
      const card = element('article', null, 'dataset-stat');
      const number = element('strong', String(value)); number.append(element('small', note));
      card.append(element('span', label), number); holder.append(card);
    });
    if (dataset.issues?.length) $('reference-explanation').textContent = `${dataset.issues.join(' ')} Dataset event positions use the vehicle GPS at annotation time; they are not surveyed hazard coordinates.`;
  }
  function renderDatasetCards() {
    const holder = $('dataset-cards');
    holder.replaceChildren();
    app.datasets.forEach((dataset) => {
      const card = element('article', null, 'panel dataset-card');
      card.append(element('h2', dataset.name || datasetName(dataset.id)));
      const missing = Number(dataset.unlabeled_recording_count) || 0;
      const unlabeled = dataset.has_labels === false || dataset.id === 'comma2k19' || (missing > 0 && missing === Number(dataset.recording_count));
      if (unlabeled) card.append(element('span', 'Unlabeled', 'badge provisional'));
      else if (missing) card.append(element('span', `${missing} recordings without labels`, 'badge neutral'));
      card.append(element('strong', `${dataset.playable_recording_count ?? 0} ready to play`));
      card.append(element('p', `${dataset.recording_count ?? 0} local recordings${unlabeled ? ' · No anomaly labels supplied' : ` · ${dataset.reference_event_count ?? 0} recorded reference events`}`));
      if (unlabeled) card.append(element('small', 'YOLO outputs are predictions. Missing labels do not indicate a hazard-free road.'));
      holder.append(card);
    });
  }
  function renderRecordingNotice() {
    const recording = selectedDatasetRecording();
    $('recording-notice').hidden = !recording;
    if (!recording) return;
    $('recording-dataset').textContent = datasetName(recordingDataset(recording));
    $('recording-label-status').textContent = unlabeledRecording() ? 'Unlabeled · No anomaly labels supplied. YOLO outputs are predictions.' : 'Anomaly references can be compared with predictions; matches remain candidates for review.';
  }
  function renderDownload(download) {
    if (!download || typeof download !== 'object') return;
    app.download = download;
    $('dataset-download').hidden = false;
    const status = String(download.status || 'idle');
    const active = ['starting', 'downloading', 'verifying', 'extracting'].includes(status);
    $('download-status').textContent = { idle: 'Ready', starting: 'Starting', downloading: 'Downloading', verifying: 'Verifying', extracting: 'Extracting', completed: 'Completed', paused: 'Paused', error: 'Download error' }[status] || titleCase(status);
    $('download-status').className = `badge ${status === 'completed' ? 'matched' : status === 'error' || status === 'paused' ? 'provisional' : 'neutral'}`;
    const progress = $('download-progress');
    if (numeric(download.bytes_total) && Number(download.bytes_total) > 0) {
      progress.max = Number(download.bytes_total);
      progress.value = Math.max(0, Math.min(Number(download.bytes_total), Number(download.bytes_downloaded) || 0));
      progress.setAttribute('aria-valuetext', `${formatBytes(download.bytes_downloaded)} of ${formatBytes(download.bytes_total)}`);
    } else { progress.removeAttribute('value'); progress.removeAttribute('aria-valuetext'); }
    $('download-bytes').textContent = `${formatBytes(download.bytes_downloaded ?? 0)}${Number(download.bytes_total) > 0 ? ` / ${formatBytes(download.bytes_total)}` : ' downloaded · total size pending'}${active && Number(download.bytes_per_second) > 0 ? ` · ${formatBytes(download.bytes_per_second)}/s` : ''}`;
    $('download-files').textContent = `${download.files_completed ?? 0}${numeric(download.files_total) ? ` / ${download.files_total}` : ''} files complete${numeric(download.segments_ready) ? ` · ${download.segments_ready} segments ready` : ''}`;
    $('download-current').textContent = download.current_file ? `Current file: ${download.current_file}` : '';
    $('download-message').textContent = download.message || (status === 'paused' ? 'Download is paused. Existing files will be reused when you resume.' : '');
    $('pause-download').hidden = !active;
    $('resume-download').hidden = active || status === 'completed';
    $('resume-download').textContent = status === 'error' ? 'Retry full download' : status === 'paused' ? 'Resume full download' : 'Download full dataset';
  }
  async function refreshDatasets(manual = false) {
    if (app.datasetsRefreshing || isRunning()) return;
    app.datasetsRefreshing = true;
    updateControls();
    try {
      const result = await api('/api/datasets/refresh', { method: 'POST' });
      applyDatasets(result);
      app.pendingDatasetRefresh = false;
      $('dataset-refresh-status').textContent = manual ? 'Recordings refreshed.' : 'New completed segments are ready.';
    } finally { app.datasetsRefreshing = false; updateControls(); }
  }
  function applyDatasets(result) {
    if (Array.isArray(result.datasets)) { app.datasets = result.datasets; renderDatasetCards(); }
    if (Array.isArray(result.recordings) && JSON.stringify(result.recordings) !== JSON.stringify(app.recordings)) {
      const before = $('recording-select').value;
      app.recordings = result.recordings;
      renderRecordings();
      const after = $('recording-select').value;
      if (after && (after !== before || !app.recordingInfo)) loadRecording(after);
    }
    if (result.dataset) renderDataset(result.dataset);
    else if (app.datasets.length) renderDataset({
      recording_count: app.datasets.reduce((sum, dataset) => sum + Number(dataset.recording_count || 0), 0),
      playable_recording_count: app.datasets.reduce((sum, dataset) => sum + Number(dataset.playable_recording_count || 0), 0),
      reference_event_count: app.datasets.reduce((sum, dataset) => sum + Number(dataset.reference_event_count || 0), 0),
      workbook_count: app.datasetSummary.workbook_count || 0,
    });
    if (result.download) renderDownload(result.download);
  }
  async function pollDatasets() {
    if (app.datasetPollBusy) return;
    app.datasetPollBusy = true;
    try {
      const result = await api('/api/datasets');
      if (result.download) renderDownload(result.download);
      const completed = result.download?.segments_ready ?? result.download?.files_completed;
      const changed = completed !== undefined && app.lastDownloadFiles !== null && completed !== app.lastDownloadFiles;
      if (completed !== undefined) app.lastDownloadFiles = completed;
      if (changed) app.pendingDatasetRefresh = true;
      if (!isRunning()) {
        applyDatasets(result);
        if (app.pendingDatasetRefresh && !Array.isArray(result.recordings)) await refreshDatasets();
        else app.pendingDatasetRefresh = false;
      } else if (Array.isArray(result.datasets)) { app.datasets = result.datasets; renderDatasetCards(); }
    } catch (_) { $('dataset-refresh-status').textContent = 'Download status unavailable. Retrying automatically.'; }
    finally {
      app.datasetPollBusy = false;
      clearTimeout(app.datasetTimer);
      const active = ['starting', 'downloading', 'verifying', 'extracting'].includes(app.download?.status);
      app.datasetTimer = setTimeout(pollDatasets, active ? 5000 : 30000);
    }
  }
  function selectDataset(value) {
    if (isRunning()) return;
    app.datasetFilter = value;
    $('dataset-select').value = value;
    $('records-dataset-select').value = value;
    renderRecordings();
    const id = $('recording-select').value;
    if (id) loadRecording(id);
    else {
      ++app.loadingRecording;
      app.loadedRecording = null; app.recordingInfo = null; app.references = []; app.track = [];
      renderTrack(); renderReferences(); renderRecordingNotice(); updateCalibrationBanner();
    }
  }
  function renderReferences() {
    $('reference-count').textContent = app.references.length;
    $('references-summary').textContent = `${app.references.length} reference records${app.loadedRecording ? ` · Recording ${app.loadedRecording}` : ''}`;
    $('reference-source').textContent = app.loadedRecording ? `Recording ${app.loadedRecording} + imported records` : 'Imported records';
    $('references-empty').hidden = app.references.length > 0;
    $('references-empty').querySelector('strong').textContent = unlabeledRecording() ? 'Unlabeled recording' : 'No reference records available';
    $('references-empty').querySelector('span').textContent = unlabeledRecording() ? 'No anomaly labels supplied. YOLO outputs are predictions; they cannot be scored against absent labels.' : 'Import an anomaly spreadsheet to compare it with detected hazards.';
    const body = $('references-body');
    const fragment = document.createDocumentFragment();
    app.references.slice(0, 500).forEach((reference) => {
      const row = element('tr');
      const point = latlng(reference);
      const proxy = reference.location_kind === 'vehicle_at_annotation';
      const id = element('td', String(reference.id || '—').slice(0, 14)); id.title = valueOr(reference.id);
      const kind = proxy ? 'Vehicle-at-event proxy' : point ? 'Supplied coordinates' : 'Time only · No GPS';
      row.append(id, element('td', className(reference)), element('td', point ? display(point[0], 6) : '—', 'coords'), element('td', point ? display(point[1], 6) : '—', 'coords'), element('td', `${numeric(reference.time_s) ? `${time(reference.time_s)} · ` : ''}${reference.recording_id ? `Recording ${reference.recording_id}` : 'Any recording'}`), element('td', kind));
      fragment.append(row);
    });
    body.replaceChildren(fragment);
    if (app.references.length > 500) $('references-summary').textContent += ' · Showing first 500';
    renderReferenceMap();
  }
  async function loadRecording(id) {
    if (!id) return;
    const requestId = ++app.loadingRecording;
    app.selectedRecording = id;
    try {
      const result = await api(`/api/recordings/${encodeURIComponent(id)}`);
      if (requestId !== app.loadingRecording) return;
      app.loadedRecording = id;
      app.recordingInfo = result.recording || app.recordings.find((recording) => String(recording.id) === String(id)) || { id };
      if (result.has_labels !== undefined) app.recordingInfo = { ...app.recordingInfo, has_labels: result.has_labels };
      app.recordingCalibration = result.calibration || null;
      app.calibrationMetadata = result.calibration_metadata || null;
      app.track = result.track || [];
      app.references = result.references || [];
      renderTrack();
      renderReferences();
      renderRecordingNotice(); updateCalibrationBanner();
      if (!isRunning()) fitMap();
    } catch (error) { toast(`Could not load recording: ${error.message}`, true); }
  }
  function createMap() {
    if (!window.L) {
      $('map-offline').hidden = false;
      $('map-offline').textContent = 'The local map library could not load. GPS coordinates remain available in the hazard table.';
      return;
    }
    app.map = L.map('hazard-map', { zoomControl: true, preferCanvas: true, minZoom: 2, maxZoom: 20 }).setView([20, 0], 3);
    const tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors' }).addTo(app.map);
    tiles.on('tileerror', () => { $('map-offline').hidden = false; });
    tiles.on('tileload', () => { app.tileSuccesses += 1; $('map-offline').hidden = true; });
    setTimeout(() => { if (!app.tileSuccesses) $('map-offline').hidden = false; }, 12000);
    app.routeLayer = L.polyline([], { color: '#649ec6', weight: 3, opacity: .65 }).addTo(app.map);
    app.referenceLayer = L.layerGroup().addTo(app.map);
    app.hazardLayer = L.layerGroup().addTo(app.map);
    L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(app.map);
  }
  function renderTrack() {
    if (!app.map || $('source-type').value !== 'dataset') return;
    const stride = Math.max(1, Math.ceil(app.track.length / 4000));
    const points = app.track.filter((_, index) => index % stride === 0).map(latlng).filter(Boolean);
    app.routeLayer.setLatLngs(points);
    if (points.length) $('map-empty').hidden = true;
  }
  function popupFor(item, reference = false) {
    const popup = element('div');
    popup.append(element('strong', `${reference ? 'Reference · ' : ''}${className(item)}`));
    const point = latlng(item);
    const lines = reference ? [`Record ${item.id || '—'}`, item.location_kind === 'vehicle_at_annotation' ? 'Vehicle-at-event proxy' : 'Imported reference'] : [`${Math.round((Number(item.confidence) || 0) * 100)}% confidence · ${item.observations || 1} observations`, `${display(item.distance_m)} m ground range`, item.provisional ? 'Provisional calibration' : 'Calibration marked verified'];
    if (!reference && item.class_ambiguous) lines.push('Class varied between frames; showing the strongest observation.');
    if (!reference && numeric(item.gps_accuracy_m)) lines.push(`Reported GPS accuracy ±${display(item.gps_accuracy_m)} m; range error is unmeasured.`);
    if (point) lines.push(`${point[0].toFixed(6)}, ${point[1].toFixed(6)}`);
    if (!reference && matched(item)) lines.push(`Reference ${item.match.reference_id} · ${display(item.match.distance_m)} m`);
    lines.forEach((line) => popup.append(element('div', line)));
    return popup;
  }
  function renderReferenceMap() {
    if (!app.map) return;
    app.referenceLayer.clearLayers();
    app.references.slice(0, 2000).forEach((reference) => {
      if ($('source-type').value !== 'dataset' && reference.location_kind === 'vehicle_at_annotation') return;
      const point = latlng(reference);
      if (!point) return;
      L.circleMarker(point, { radius: 5, color: '#d8ad69', fillColor: '#eab96b', weight: 1.5, fillOpacity: .28 }).bindPopup(popupFor(reference, true)).addTo(app.referenceLayer);
    });
  }
  function fitMap() {
    if (!app.map) return;
    const points = [];
    if ($('source-type').value === 'dataset') {
      const stride = Math.max(1, Math.ceil(app.track.length / 1000));
      app.track.filter((_, index) => index % stride === 0).forEach((item) => { const point = latlng(item); if (point) points.push(point); });
      app.references.forEach((item) => { const point = latlng(item); if (point) points.push(point); });
    } else app.references.forEach((item) => { if (item.location_kind !== 'vehicle_at_annotation') { const point = latlng(item); if (point) points.push(point); } });
    (app.state.hazards || []).forEach((item) => { const point = latlng(item); if (point) points.push(point); });
    const pose = latlng(app.state.pose); if (pose) points.push(pose);
    if (points.length) { app.map.fitBounds(L.latLngBounds(points), { padding: [30, 30], maxZoom: 18 }); $('map-empty').hidden = true; }
  }
  function updateMap() {
    if (!app.map) return;
    const hazards = (app.state.hazards || []).slice(-1000);
    const keep = new Set();
    hazards.forEach((hazard, index) => {
      const point = latlng(hazard); if (!point) return;
      const key = String(hazard.id ?? `hazard-${index}`); keep.add(key);
      let marker = app.markers.get(key);
      if (!marker) {
        const icon = L.divIcon({ className: 'hazard-marker', html: `<div class="hazard-pin${hazard.provisional ? ' provisional' : ''}"></div>`, iconSize: [17, 17], iconAnchor: [8, 8] });
        marker = L.marker(point, { icon }).addTo(app.hazardLayer);
        app.markers.set(key, marker);
      } else marker.setLatLng(point);
      const signature = JSON.stringify(hazard);
      if (marker.roadwatchSignature !== signature) {
        if (marker.getPopup()) marker.setPopupContent(popupFor(hazard));
        else marker.bindPopup(popupFor(hazard));
        marker.roadwatchSignature = signature;
      }
    });
    app.markers.forEach((marker, key) => { if (!keep.has(key)) { app.hazardLayer.removeLayer(marker); app.markers.delete(key); } });
    $('map-hazard-count').textContent = keep.size;
    const pose = app.state.pose;
    const point = latlng(pose);
    if (point) {
      if (!app.vehicle) {
        app.vehicle = L.marker(point, { icon: L.divIcon({ className: 'vehicle-icon', html: '<div class="vehicle-pin">↑</div>', iconSize: [25, 25], iconAnchor: [12, 12] }), zIndexOffset: 1000 }).addTo(app.map);
        app.vehicle.bindTooltip('Camera vehicle');
      } else app.vehicle.setLatLng(point);
      const pin = app.vehicle.getElement()?.querySelector('.vehicle-pin');
      if (pin) {
        pin.textContent = numeric(pose.heading_deg) ? '↑' : '•';
        pin.style.transform = `rotate(${numeric(pose.heading_deg) ? Number(pose.heading_deg) : 0}deg)`;
      }
      if ($('map-follow').checked && isRunning()) app.map.setView(point, Math.max(app.map.getZoom(), 17), { animate: false });
      $('map-empty').hidden = true;
    } else if (app.vehicle) { app.map.removeLayer(app.vehicle); app.vehicle = null; }
    if (keep.size) $('map-empty').hidden = true;
  }
  function renderHazards() {
    const all = app.state.hazards || [];
    const filter = $('hazard-filter').value;
    const filtered = [...all].reverse().filter((hazard) => filter === 'matched' ? matched(hazard) : filter === 'unmatched' ? !matched(hazard) : filter === 'located' ? Boolean(latlng(hazard)) : true);
    const fragment = document.createDocumentFragment();
    filtered.slice(0, 300).forEach((hazard) => {
      const row = element('tr');
      const name = element('td'); const nameWrap = element('div', null, 'hazard-name');
      const detail = element('div'); detail.append(element('strong', className(hazard)), element('small', `#${String(hazard.id ?? '—').slice(0, 16)}`));
      if (hazard.class_ambiguous) detail.append(element('small', 'Class varied across frames'));
      nameWrap.append(element('i', '⌁'), detail); name.append(nameWrap);
      const confidence = element('td'); const confidenceBar = element('span', `${Math.round((Number(hazard.confidence) || 0) * 100)}%`, 'confidence-bar');
      confidenceBar.style.setProperty('--confidence', `${Math.max(0, Math.min(100, (Number(hazard.confidence) || 0) * 100))}%`); confidence.append(confidenceBar);
      const distance = element('td'); distance.append(element('strong', numeric(hazard.distance_m) ? `${display(hazard.distance_m)} m` : 'Unavailable'));
      if (hazard.provisional) distance.append(element('small', 'Provisional estimate'));
      const coords = element('td', null, 'coords'); const point = latlng(hazard);
      if (point) { coords.append(element('div', point[0].toFixed(6)), element('div', point[1].toFixed(6))); coords.title = numeric(hazard.gps_accuracy_m) ? `Reported GPS accuracy ±${display(hazard.gps_accuracy_m)} m; calibration and range error are unmeasured.` : 'GPS accuracy is unknown; calibration and range error are unmeasured.'; }
      else { coords.textContent = 'GPS unavailable'; coords.title = hazard.projection_reason || 'A valid GPS position and heading are required.'; }
      const match = element('td');
      if (matched(hazard)) {
        const details = hazard.match;
        match.append(element('span', details.match_kind === 'temporal' ? 'Time candidate' : 'Matched candidate', 'badge matched'));
        match.append(element('small', numeric(details.distance_m) ? `${display(details.distance_m)} m · ${String(details.reference_id || '').slice(0, 10)}` : `Δ ${display(details.time_delta_s)} s`));
        match.title = `Reference ${details.reference_id || '—'} · ${details.match_kind || 'candidate'}`;
      } else match.append(element('span', unlabeledRecording() && !app.references.length ? 'Unlabeled' : 'No match', 'badge neutral'));
      row.append(name, confidence, distance, coords, match, element('td', String(hazard.observations || 1)), element('td', time(hazard.last_seen_s)));
      if (point) { row.style.cursor = 'pointer'; row.title = 'Show this hazard on the map'; row.tabIndex = 0; const focus = () => { setTab('live'); app.map?.setView(point, 19); app.markers.get(String(hazard.id))?.openPopup(); $('map-follow').checked = false; $('hazard-map').scrollIntoView({ block: 'center' }); }; row.addEventListener('click', focus); row.addEventListener('keydown', (event) => { if (event.key === 'Enter') focus(); }); }
      fragment.append(row);
    });
    $('hazards-body').replaceChildren(fragment);
    $('hazards-empty').hidden = filtered.length > 0;
    if (!filtered.length) {
      $('hazards-empty').querySelector('strong').textContent = all.length ? 'No hazards match this filter' : 'No hazards detected yet';
      $('hazards-empty').querySelector('span').textContent = all.length ? 'Choose another filter to inspect the detected hazards.' : 'Unique hazards will appear here with distance, coordinates, and record matches.';
    }
    $('table-hazard-count').textContent = all.length;
    $('table-summary').textContent = `${all.length} ${all.length === 1 ? 'hazard' : 'hazards'} in this session${filter !== 'all' ? ` · ${filtered.length} matching filter` : ''}${filtered.length > 300 ? ' · Showing latest 300' : ''}`;
  }
  function renderState(state) {
    window.dispatchEvent(new CustomEvent('roadwatch:state', { detail: state }));
    if (!state || typeof state !== 'object') return;
    const previousSession = app.session;
    app.state = state;
    app.session = state.session_id;
    $$('.export-button').forEach((link) => { link.href = state.session_id ? `/api/export.csv?session_id=${encodeURIComponent(state.session_id)}` : '/api/export.csv'; });
    if (previousSession !== app.session) {
      app.markers.forEach((marker) => app.hazardLayer?.removeLayer(marker)); app.markers.clear();
      if (app.vehicle) { app.map?.removeLayer(app.vehicle); app.vehicle = null; }
      app.lastHazardSignature = ''; app.lastMapSignature = '';
      if (state.session_id && app.bootstrapped && state.source_type) {
        $('source-type').value = state.source_type;
        if (state.pose_mode) $('pose-mode').value = state.pose_mode;
        if (state.source_type === 'dataset' && state.recording_id) {
          const recording = app.recordings.find((item) => String(item.id) === String(state.recording_id));
          if (recording) {
            if (app.datasetFilter !== 'all' && app.datasetFilter !== recordingDataset(recording)) {
              app.datasetFilter = recordingDataset(recording);
              $('dataset-select').value = app.datasetFilter; $('records-dataset-select').value = app.datasetFilter;
              renderRecordings();
            }
            $('recording-select').value = String(state.recording_id);
            app.selectedRecording = String(state.recording_id);
            loadRecording(state.recording_id);
          }
        }
        updateSourceFields();
      }
    }
    updateControls();
    const detections = state.detections || [];
    const hazards = state.hazards || [];
    const ranges = detections.map((detection) => detection.distance_m).filter(numeric).map(Number);
    $('metric-hazards').textContent = hazards.length;
    $('metric-observations').textContent = `${state.total_detections || 0} observations`;
    $('metric-distance').replaceChildren(document.createTextNode(ranges.length ? Math.min(...ranges).toFixed(1) : '—'), element('small', ' m'));
    const provisional = detections.some((detection) => detection.provisional) || recordingDataset(selectedDatasetRecording()) === 'comma2k19' || !app.config?.calibration?.verified;
    $('metric-distance-note').textContent = ranges.length ? (provisional ? 'Provisional · verify mounting height' : 'Calibrated road-plane estimate') : detections.length ? 'Ground projection unavailable' : 'Waiting for a hazard';
    $('metric-matches').textContent = unlabeledRecording() && !app.references.length ? '—' : hazards.filter(matched).length;
    $('metric-fps').replaceChildren(document.createTextNode(numeric(state.fps) && state.fps > 0 ? display(state.fps) : '—'), element('small', ' fps'));
    $('video-status').textContent = isRunning() ? 'LIVE' : state.status === 'completed' ? 'COMPLETE' : state.status === 'error' ? 'ERROR' : state.status === 'stopped' ? 'STOPPED' : 'IDLE';
    $('video-status').classList.toggle('active', isRunning()); $('video-status').classList.toggle('error', state.status === 'error');
    const hasFrame = Number(state.frame_index) > 0 || Number(state.fps) > 0;
    $('frame-meta').textContent = hasFrame ? `${valueOr(state.frame_width)} × ${valueOr(state.frame_height)} · Frame ${state.frame_index}` : isRunning() ? 'Opening source…' : 'No active source';
    const frame = $('live-frame');
    if (state.session_id && (isRunning() || hasFrame) && app.videoSession !== state.session_id) {
      app.videoSession = state.session_id; frame.hidden = true; $('video-empty').hidden = false; $('video-overlay').hidden = true;
      frame.src = `/video.mjpg?session=${encodeURIComponent(state.session_id)}`;
    }
    if (hasFrame) { frame.hidden = false; $('video-empty').hidden = true; $('video-overlay').hidden = false; }
    $('video-time').textContent = `${time(state.video_time_s)} · ${detections.length} in frame`;
    const pose = state.pose; const point = latlng(pose);
    $('video-pose-status').textContent = point ? numeric(pose.heading_deg) ? `GPS · ${display(pose.heading_deg, 0)}°` : 'GPS · heading unavailable' : 'GPS unavailable';
    $('gps-readout').textContent = point ? `GPS ${point[0].toFixed(5)}, ${point[1].toFixed(5)} · ${numeric(pose.accuracy_m) ? `±${display(pose.accuracy_m)} m` : 'accuracy unknown'}` : 'GPS — waiting for position & heading';
    $('pipeline-status').textContent = { idle: 'Ready to inspect', running: 'Detection running', stopped: 'Session stopped', completed: 'Recording complete', error: 'Session error' }[state.status] || 'Ready';
    $('pipeline-dot').className = `status-dot${isRunning() ? ' running' : state.status === 'error' ? ' error' : ''}`;
    $('session-message').textContent = state.message || '';
    if (state.model) renderModel(state.model);
    if (state.download) renderDownload(state.download);
    if (!isRunning() && app.pendingDatasetRefresh && !app.datasetPollBusy && !app.datasetsRefreshing) pollDatasets();
    const now = performance.now();
    if (now - app.lastRender > 450 || !isRunning()) {
      const hazardSignature = JSON.stringify(hazards);
      const mapSignature = hazardSignature + JSON.stringify(state.pose);
      if (hazardSignature !== app.lastHazardSignature) { renderHazards(); app.lastHazardSignature = hazardSignature; }
      if (mapSignature !== app.lastMapSignature) { updateMap(); app.lastMapSignature = mapSignature; }
      app.lastRender = now;
    }
  }
  function startConnection() {
    if (app.socket) app.socket.close();
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
    app.socket = socket;
    socket.addEventListener('open', () => { connection('Live connection'); if (app.pollTimer) { clearInterval(app.pollTimer); app.pollTimer = null; } });
    socket.addEventListener('message', (event) => { try { renderState(JSON.parse(event.data)); } catch (_) { /* Ignore malformed transient frames. */ } });
    socket.addEventListener('close', () => {
      connection('Reconnecting', false);
      if (!app.pollTimer) app.pollTimer = setInterval(async () => {
        try { renderState(await api('/api/state')); connection('Polling connection'); } catch (_) { connection('Server offline', false); }
      }, 1500);
      clearTimeout(app.reconnectTimer); app.reconnectTimer = setTimeout(startConnection, 5000);
    });
    socket.addEventListener('error', () => socket.close());
  }
  function poseFromInputs() {
    const values = { latitude: $('pose-latitude').value, longitude: $('pose-longitude').value, heading_deg: $('pose-heading').value, accuracy_m: $('pose-accuracy').value };
    if (!Object.values(values).every(numeric)) throw new Error('Enter latitude, longitude, vehicle heading, and GPS accuracy. Heading cannot be inferred from a stationary position.');
    const pose = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Number(value)]));
    if (Math.abs(pose.latitude) > 90 || Math.abs(pose.longitude) > 180 || pose.heading_deg < 0 || pose.heading_deg > 360 || pose.accuracy_m < 0) throw new Error('Check latitude (±90), longitude (±180), heading (0–360°), and positive GPS accuracy.');
    return pose;
  }
  async function sendBrowserPose(position) {
    const coords = position.coords;
    $('pose-latitude').value = coords.latitude;
    $('pose-longitude').value = coords.longitude;
    $('pose-accuracy').value = coords.accuracy;
    const measuredHeading = numeric(coords.heading) ? Number(coords.heading) : null;
    if (measuredHeading !== null) $('pose-heading').value = measuredHeading.toFixed(1);
    if (!numeric($('pose-heading').value)) {
      $('pose-help').textContent = 'GPS position received, but the device has no travel heading. Enter the measured vehicle heading above; fixes will then be forwarded on the next update.';
      return;
    }
    const pose = { latitude: coords.latitude, longitude: coords.longitude, heading_deg: measuredHeading ?? Number($('pose-heading').value), accuracy_m: coords.accuracy, timestamp_ms: position.timestamp };
    app.geoPose = pose;
    if (Date.now() - app.lastGeoSend < 700) return;
    app.lastGeoSend = Date.now();
    try { await api('/api/pose', { method: 'POST', body: pose }); $('pose-help').textContent = `Browser GPS connected · ±${coords.accuracy.toFixed(1)} m · ${measuredHeading === null ? 'Using manually entered heading' : 'Using device travel heading'}. Keep this page open.`; }
    catch (error) { $('pose-help').textContent = `GPS update rejected: ${error.message}`; }
  }
  function bindEvents() {
    $$('.nav-item').forEach((button) => button.addEventListener('click', () => setTab(button.dataset.tab)));
    $$('[data-go]').forEach((button) => button.addEventListener('click', () => { setTab(button.dataset.go); window.scrollTo({ top: 0, behavior: 'smooth' }); }));
    window.addEventListener('hashchange', () => setTab(location.hash.slice(1)));
    $('source-type').addEventListener('change', () => {
      const type = $('source-type').value;
      if (type === 'camera') $('source-path').value = '0';
      else if (type !== 'dataset') $('source-path').value = '';
      if (type === 'dataset') $('pose-mode').value = 'dataset';
      updateSourceFields();
      if (type === 'dataset' && $('recording-select').value) loadRecording($('recording-select').value);
    });
    $('pose-mode').addEventListener('change', updateSourceFields);
    $('dataset-select').addEventListener('change', (event) => selectDataset(event.target.value));
    $('records-dataset-select').addEventListener('change', (event) => selectDataset(event.target.value));
    $('refresh-datasets').addEventListener('click', () => busy($('refresh-datasets'), () => refreshDatasets(true), 'Refreshing…'));
    $('pause-download').addEventListener('click', () => busy($('pause-download'), async () => {
      const result = await api('/api/datasets/download/stop', { method: 'POST' });
      renderDownload(result.download || result);
      await pollDatasets();
    }, 'Pausing…'));
    $('resume-download').addEventListener('click', () => busy($('resume-download'), async () => {
      const result = await api('/api/datasets/download/start', { method: 'POST', body: { scope: 'full' } });
      renderDownload(result.download || result);
      await pollDatasets();
    }, 'Starting…'));
    $('recording-select').addEventListener('change', () => loadRecording($('recording-select').value));
    $('session-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (app.busy || isRunning()) return;
      await busy($('start-button'), async () => {
        const kind = $('source-type').value; const poseMode = $('pose-mode').value;
        if (kind === 'dataset' && !$('recording-select').value) throw new Error('Choose a recording with a video file.');
        if (kind !== 'dataset' && !$('source-path').value.trim()) throw new Error('Enter the camera index, stream URL, or file path.');
        if (poseMode === 'manual') {
          const pose = poseFromInputs();
          const config = structuredClone(app.config); config.pose = { ...config.pose, ...pose, mode: 'manual' };
          app.config = await api('/api/config', { method: 'POST', body: config });
        }
        const result = await api('/api/session/start', { method: 'POST', body: { source_type: kind, recording_id: $('recording-select').value, source: $('source-path').value.trim(), pose_mode: poseMode } });
        renderState(result);
        if (poseMode === 'live' && app.geoPose) {
          try { await api('/api/pose', { method: 'POST', body: app.geoPose }); }
          catch (error) { toast(`Detection started. GPS needs a fresh fix: ${error.message}`, true); }
        }
        // Model class metadata becomes available after the worker loads weights.
        setTimeout(async () => { try { const boot = await api('/api/bootstrap'); renderModel(boot.model); } catch (_) {} }, 5000);
      }, 'Starting…');
    });
    $('stop-button').addEventListener('click', () => busy($('stop-button'), async () => { renderState(await api('/api/session/stop', { method: 'POST' })); }, 'Stopping…'));
    $('config-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (isRunning()) { toast('Stop detection before changing camera calibration or inference settings.', true); return; }
      const button = event.submitter || $('config-form').querySelector('[type="submit"]');
      await busy(button, async () => {
        const result = await api('/api/config', { method: 'POST', body: configFromForm() });
        app.config = result.config || result;
        populateConfig(); renderModel({ path: app.config.detection.model_path, status: 'Load on start' });
        toast('Configuration saved. Start a new detection session to use these settings.');
      }, 'Saving…');
    });
    $('checkerboard-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = event.submitter;
      await busy(button, async () => {
        const data = new FormData(event.target);
        if (event.target.elements.images.files.length < 3) throw new Error('Select at least three checkerboard images from different angles. More varied views improve lens calibration.');
        const result = await api('/api/calibration/checkerboard', { method: 'POST', body: data });
        const calibration = result.calibration || {};
        Object.entries(calibration).forEach(([key, value]) => {
          const input = document.querySelector(`[data-config="calibration.${key}"]`);
          if (input) input.value = value;
          if (key === 'distortion') $('distortion-input').value = value.join(', ');
        });
        $('calibration-verified').checked = false;
        $('checkerboard-result').textContent = `Reprojection RMS: ${display(result.rms_px, 3)} px. Intrinsics populated above. Verify mounting geometry, then save configuration.`;
        toast('Checkerboard intrinsics calculated. Review the values above and save configuration.');
      }, 'Calibrating…');
    });
    $('pose-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      await busy(event.submitter, async () => {
        const pose = poseFromInputs();
        if ($('pose-mode').value === 'manual') {
          if (isRunning()) throw new Error('Stop detection before changing the fixed manual position.');
          const config = structuredClone(app.config); config.pose = { ...config.pose, ...pose, mode: 'manual' };
          app.config = await api('/api/config', { method: 'POST', body: config });
          toast('Manual vehicle position and heading saved. This position remains fixed during a session.');
        } else { await api('/api/pose', { method: 'POST', body: pose }); toast('GPS fix sent. Live fixes must continue arriving within the configured maximum age.'); }
      }, 'Saving…');
    });
    $('browser-gps-button').addEventListener('click', () => {
      if (app.geoWatch !== null) { navigator.geolocation.clearWatch(app.geoWatch); app.geoWatch = null; app.geoPose = null; $('browser-gps-button').textContent = 'Use browser GPS'; $('pose-help').textContent = 'Browser GPS stopped. Connect a GPS bridge or send a new fix to continue geolocation.'; return; }
      if (!navigator.geolocation) { toast('This browser does not support geolocation. Use a GPS bridge to POST /api/pose.', true); return; }
      $('browser-gps-button').textContent = 'Stop browser GPS';
      app.geoWatch = navigator.geolocation.watchPosition(sendBrowserPose, (error) => { toast(`Browser GPS: ${error.message}`, true); $('pose-help').textContent = `GPS unavailable: ${error.message}`; }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
    });
    $('hazard-filter').addEventListener('change', renderHazards);
    $('map-fit').addEventListener('click', () => { $('map-follow').checked = false; fitMap(); });
    $('map-follow').addEventListener('change', updateMap);
    $('reference-file').addEventListener('change', () => { $('upload-label').textContent = $('reference-file').files[0]?.name || 'Choose a spreadsheet'; });
    $('import-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      await busy(event.submitter, async () => {
        const file = $('reference-file').files[0]; if (!file) throw new Error('Choose an Excel workbook or CSV file.');
        const data = new FormData(); data.append('file', file);
        const mapping = {};
        $$('[data-mapping]').forEach((input) => { if (input.value.trim()) mapping[input.dataset.mapping] = input.value.trim(); });
        data.append('mapping', JSON.stringify(mapping));
        const result = await api('/api/references/import', { method: 'POST', body: data });
        $('import-result').textContent = `${result.records?.length ?? 0} reference records imported.${result.columns?.length ? `\nColumns: ${result.columns.join(', ')}` : ''}${result.issues?.length ? `\n${result.issues.join('\n')}` : ''}`;
        if ($('recording-select').value) await loadRecording($('recording-select').value);
        else { app.references = result.records || []; renderReferences(); }
        toast(result.records?.length ? 'Reference records imported. Start a new session to use the updated matches.' : 'No usable records were imported. Review the column mapping and messages below.', !result.records?.length);
      }, 'Importing…');
    });
    $('probe-toggle').addEventListener('click', () => {
      app.probe = !app.probe; $('live-frame').classList.toggle('probing', app.probe);
      $('probe-result').hidden = !app.probe;
      $('probe-result').textContent = app.probe ? 'Click a visible road point in the camera image to inspect its projection using the saved calibration.' : '';
      $('probe-toggle').textContent = app.probe ? 'Exit point inspector ×' : 'Inspect a road point ⌖';
    });
    $('live-frame').addEventListener('click', async (event) => {
      if (!app.probe || !(Number(app.state.frame_index) > 0 || Number(app.state.fps) > 0)) return;
      const image = event.currentTarget;
      const bounds = image.getBoundingClientRect();
      const width = app.state.frame_width || image.naturalWidth; const height = app.state.frame_height || image.naturalHeight;
      const scale = Math.min(bounds.width / width, bounds.height / height);
      const left = bounds.left + (bounds.width - width * scale) / 2; const top = bounds.top + (bounds.height - height * scale) / 2;
      const rawU = (event.clientX - left) / scale; const rawV = (event.clientY - top) / scale;
      if (rawU < 0 || rawV < 0 || rawU >= width || rawV >= height) return;
      const u = Math.max(0, Math.min(width - 1, rawU));
      const v = Math.max(0, Math.min(height - 1, rawV));
      try {
        const result = await api('/api/calibration/probe', { method: 'POST', body: { u, v, frame_width: width, frame_height: height } });
        const projection = result.projection || result; const location = result.location || {};
        $('probe-result').textContent = projection.valid === false ? `Pixel (${Math.round(u)}, ${Math.round(v)}): ${projection.reason || 'No valid ground intersection'}` : `Pixel (${Math.round(u)}, ${Math.round(v)}): ${display(projection.distance_m)} m range · ${display(projection.forward_m)} m forward · ${display(projection.right_m)} m right. ${latlng(result) ? `GPS ${display(result.latitude, 6)}, ${display(result.longitude, 6)}` : location.reason || 'GPS unavailable'}${app.config.calibration.verified ? '' : ' · Provisional calibration'}`;
      } catch (error) { $('probe-result').textContent = error.message; }
    });
    $('live-frame').addEventListener('error', () => { if (isRunning()) $('session-message').textContent = 'Camera image stream disconnected. Waiting for a frame.'; });
  }
  async function init() {
    bindEvents(); setTab(location.hash.slice(1) || 'live'); createMap();
    try {
      const bootstrap = await api('/api/bootstrap');
      app.config = bootstrap.config;
      app.state = bootstrap.state || {};
      app.recordings = bootstrap.recordings || [];
      app.datasets = bootstrap.datasets || [];
      app.download = bootstrap.download || null;
      if (app.download) app.lastDownloadFiles = app.download.segments_ready ?? app.download.files_completed ?? null;
      app.references = bootstrap.references || [];
      app.bootstrapped = true;
      populateConfig(); renderRecordings();
      if (app.state.session_id) {
        if (app.state.source_type) $('source-type').value = app.state.source_type;
        if (app.state.pose_mode) $('pose-mode').value = app.state.pose_mode;
        updateSourceFields();
      }
      renderDataset(bootstrap.dataset); renderDatasetCards(); renderDownload(bootstrap.download); renderModel(bootstrap.model); renderReferences(); renderState(app.state);
      if ($('recording-select').value) loadRecording($('recording-select').value);
      startConnection();
      app.datasetTimer = setTimeout(pollDatasets, 5000);
    } catch (error) {
      connection('Server unavailable', false);
      $('session-message').textContent = 'Unable to load the local server. Refresh this page once the server is running.';
      $('model-name').textContent = 'Model unavailable'; $('model-class-count').textContent = 'Connect to the RoadWatch server'; $('model-status').textContent = 'Offline';
      $('recording-select').replaceChildren(element('option', 'Server unavailable'));
      toast(`Could not load RoadWatch: ${error.message}`, true);
    }
    updateControls();
  }
  init();
})();
