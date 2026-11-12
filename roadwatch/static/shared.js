import { reactive } from './vendor/vue.esm-browser.prod.js';

export const tabs = [
  ['live', 'Live monitor'],
  ['submit', 'Report a hazard'],
  ['replay', 'Detection replay'],
  ['calibration', 'Camera calibration'],
  ['records', 'Dataset & records'],
];

export const dashboard = reactive({
  tab: 'live',
  fromVideoUpload: false,
  ready: false,
  config: null,
  state: {},
  model: {},
  recordings: [],
  datasets: [],
  dataset: {},
  download: null,
  references: [],
  referencesVersion: 0,
  datasetFilter: 'all',
  datasetRefreshStatus: '',
  datasetsRefreshing: false,
  selectedRecording: '',
  selectedRecordingDetails: null,
  openRecordingRequest: null,
  replayRequest: null,
  connectionLabel: 'Connecting',
  connectionOnline: false,
  toast: null,
});

export function go(tab, fromVideoUpload = false) {
  const next = tabs.some(([name]) => name === tab) ? tab : 'live';
  const changed = dashboard.tab !== next;
  dashboard.tab = next;
  dashboard.fromVideoUpload = next === 'calibration' && fromVideoUpload;
  if (location.hash !== `#${next}`) history.replaceState(null, '', `#${next}`);
  if (changed) window.scrollTo(0, 0);
}

export function requestReplay(source, startAt = 0, fallback = null) {
  dashboard.replayRequest = { source, startAt, fallback };
  go('replay');
}

let toastTimer;
export function notify(message, error = false) {
  clearTimeout(toastTimer);
  dashboard.toast = { message, error };
  toastTimer = setTimeout(() => { dashboard.toast = null; }, error ? 12000 : 6500);
}

export async function api(url, options = {}) {
  const request = { ...options, headers: new Headers(options.headers) };
  if (request.body && !(request.body instanceof FormData)) {
    request.headers.set('Content-Type', 'application/json');
    request.body = JSON.stringify(request.body);
  }
  const response = await fetch(url, request);
  const payload = await response.json().catch(() => null);
  if (response.ok) return payload;
  const detail = payload?.detail;
  const message = typeof detail === 'string' ? detail
    : Array.isArray(detail) ? detail.map((entry) => entry.msg).join('; ')
      : `Request failed (${response.status}).`;
  throw new Error(message);
}

export function datasetName(id) {
  return dashboard.datasets.find((dataset) => dataset.id === id)?.name
    || ({ roadsens: 'RoadSens-4M', comma2k19: 'comma2k19' }[id] ?? id);
}

function applyDatasets(result) {
  if (result.datasets) dashboard.datasets = result.datasets;
  if (result.dataset) dashboard.dataset = result.dataset;
  if (result.download) dashboard.download = result.download;
  if (result.recordings && dashboard.state.status !== 'running'
      && JSON.stringify(result.recordings) !== JSON.stringify(dashboard.recordings)) {
    dashboard.recordings = result.recordings;
  }
}

let refreshRequest;
export function refreshDatasets() {
  if (refreshRequest) return refreshRequest;
  dashboard.datasetsRefreshing = true;
  refreshRequest = api('/api/datasets/refresh', { method: 'POST' }).then((result) => {
    applyDatasets(result);
    dashboard.datasetRefreshStatus = 'Recordings refreshed.';
    return result;
  }).finally(() => {
    refreshRequest = null;
    dashboard.datasetsRefreshing = false;
  });
  return refreshRequest;
}

export function startDashboard() {
  let socket;
  let stopped = false;
  let pollingState = false;
  let pollTimer;
  let reconnectTimer;
  let datasetTimer;

  function connection(label, online = false) {
    dashboard.connectionLabel = label;
    dashboard.connectionOnline = online;
  }

  async function pollState() {
    if (pollingState) return;
    pollingState = true;
    try {
      const result = await api('/api/state');
      if (!stopped && socket?.readyState !== WebSocket.OPEN) {
        dashboard.state = result;
        connection('Polling connection', true);
      }
    } catch (_) {
      if (!stopped && socket?.readyState !== WebSocket.OPEN) connection('Server offline');
    } finally { pollingState = false; }
  }

  function connect() {
    if (stopped) return;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${location.host}/ws`);
    socket.addEventListener('open', () => {
      connection('Live connection', true);
      clearInterval(pollTimer);
      pollTimer = null;
    });
    socket.addEventListener('message', (event) => {
      try { dashboard.state = JSON.parse(event.data); } catch (_) { /* Keep the last valid state. */ }
    });
    socket.addEventListener('error', () => socket.close());
    socket.addEventListener('close', () => {
      if (stopped) return;
      connection('Reconnecting');
      if (!pollTimer) pollTimer = setInterval(pollState, 1500);
      reconnectTimer = setTimeout(connect, 5000);
    });
  }

  async function pollDatasets() {
    try {
      if (!refreshRequest) {
        const result = await api('/api/datasets');
        if (!stopped && !refreshRequest) applyDatasets(result);
      }
    } catch (_) {
      dashboard.datasetRefreshStatus = 'Download status unavailable. Retrying automatically.';
    } finally {
      const downloading = ['starting', 'downloading', 'verifying', 'extracting'].includes(dashboard.download?.status);
      if (!stopped) datasetTimer = setTimeout(pollDatasets, downloading ? 5000 : 30000);
    }
  }

  api('/api/bootstrap').then((bootstrap) => {
    if (stopped) return;
    dashboard.config = bootstrap.config;
    dashboard.recordings = bootstrap.recordings || [];
    dashboard.datasets = bootstrap.datasets || [];
    dashboard.dataset = bootstrap.dataset || {};
    dashboard.download = bootstrap.download || null;
    dashboard.references = bootstrap.references || [];
    dashboard.model = bootstrap.model || {};
    dashboard.state = bootstrap.state || {};
    dashboard.selectedRecording = String(bootstrap.state?.recording_id || '');
    dashboard.ready = true;
    connect();
    datasetTimer = setTimeout(pollDatasets, 5000);
  }).catch((error) => {
    if (stopped) return;
    connection('Server unavailable');
    notify(`Could not load RoadWatch: ${error.message}`, true);
  });

  return () => {
    stopped = true;
    clearTimeout(datasetTimer);
    clearTimeout(reconnectTimer);
    clearInterval(pollTimer);
    socket?.close();
  };
}
