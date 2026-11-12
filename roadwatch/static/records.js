import { computed, reactive, ref } from './vendor/vue.esm-browser.prod.js';
import { api, dashboard, datasetName, go, notify, refreshDatasets } from './shared.js';

const mappingFields = [
  ['latitude', 'LATITUDE COLUMN', 'latitude'],
  ['longitude', 'LONGITUDE COLUMN', 'longitude'],
  ['hazard_type', 'HAZARD CLASS COLUMN', 'anomaly'],
  ['time_s', 'TIME COLUMN · SECONDS', 'time_s'],
  ['recording_id', 'RECORDING ID COLUMN', 'recording_id'],
  ['id', 'RECORD ID COLUMN', 'id'],
  ['sheet', 'WORKSHEET NAME', 'All worksheets'],
];
const downloadLabels = {
  idle: 'Ready', starting: 'Starting', downloading: 'Downloading', verifying: 'Verifying',
  extracting: 'Extracting', completed: 'Completed', paused: 'Paused', error: 'Download error',
};

function numeric(value) {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
}

function formatBytes(value) {
  if (!numeric(value)) return 'Unknown size';
  const bytes = Math.max(0, Number(value));
  if (bytes < 1024) return Math.round(bytes) + ' B';
  const unit = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  return (bytes / (1024 ** unit)).toFixed(unit > 1 ? 2 : 1) + ' ' + ['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit];
}

function className(reference) {
  return String(reference.class_name ?? reference.hazard_type ?? reference.type ?? 'Unknown')
    .replace(/[_-]/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

function time(value) {
  const seconds = Math.max(0, Math.floor(Number(value)));
  return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
}

function position(reference) {
  const lat = reference.latitude ?? reference.lat ?? reference.location_latitude;
  const lon = reference.longitude ?? reference.lon ?? reference.lng ?? reference.location_longitude;
  return numeric(lat) && numeric(lon) && Math.abs(Number(lat)) <= 90 && Math.abs(Number(lon)) <= 180
    ? [Number(lat).toFixed(6), Number(lon).toFixed(6)] : null;
}

const recordingDataset = recording => recording?.dataset_id || 'roadsens';
const unlabeled = recording => Boolean(recording && (recording.has_labels === false || recordingDataset(recording) === 'comma2k19'));

const RECORDS_TEMPLATE = `
  <section id="tab-records" class="tab-panel" aria-labelledby="records-title">
    <div class="page-heading">
      <div><div class="eyebrow">CONNECT YOUR GROUND TRUTH</div><h1 id="records-title">Dataset & records</h1>
        <p>Explore your recordings and compare detections with recorded anomalies.</p>
      </div>
      <a class="button secondary" href="/api/export.csv">Export detections</a>
    </div>
    <div id="dataset-summary" class="dataset-summary">
      <article v-for="[label, count, detail] in stats" :key="label" class="dataset-stat">
        <span>{{ label }}</span><strong>{{ count }}<small>{{ detail }}</small></strong>
      </article>
    </div>
    <div id="dataset-cards" class="dataset-cards">
      <article v-for="dataset in datasetCards" :key="dataset.id" class="panel dataset-card">
        <h2>{{ dataset.name || datasetName(dataset.id) }}</h2>
        <span v-if="dataset.unlabeled" class="badge provisional">Unlabeled</span>
        <span v-else-if="dataset.missing" class="badge neutral">{{ dataset.missing }} recordings without labels</span>
        <strong>{{ dataset.playable_recording_count ?? 0 }} ready to play</strong>
        <p>{{ dataset.recording_count ?? 0 }} local recordings · {{ dataset.unlabeled ? 'No anomaly labels supplied' : (dataset.reference_event_count ?? 0) + ' recorded reference events' }}</p>
        <small v-if="dataset.unlabeled">YOLO outputs are predictions. Missing labels do not indicate a hazard-free road.</small>
      </article>
    </div>
    <article v-if="dashboard.download" id="dataset-download" class="panel download-panel" aria-labelledby="download-title">
      <div class="download-heading">
        <div><div class="eyebrow">OFFICIAL DATASET FILES</div><h2 id="download-title">comma2k19 download</h2></div>
        <span id="download-status" class="badge" :class="download.status === 'completed' ? 'matched' : ['error', 'paused'].includes(download.status) ? 'provisional' : 'neutral'">{{ download.label }}</span>
      </div>
      <progress v-if="download.total" id="download-progress" aria-label="Dataset download progress"
        :aria-valuetext="download.progressLabel" :max="download.total" :value="download.progress"></progress>
      <progress v-else id="download-progress" aria-label="Dataset download progress"></progress>
      <div class="download-stats"><span id="download-bytes">{{ download.bytesLabel }}</span><span id="download-files">{{ download.filesLabel }}</span></div>
      <p id="download-current" class="helper">{{ download.currentFile }}</p>
      <p id="download-message" class="helper" role="status">{{ download.message }}</p>
      <p class="helper">Completed segments become available while the remaining dataset downloads. These recordings have no anomaly labels; YOLO detections are predictions.</p>
      <div class="download-actions">
        <button v-show="download.active" id="pause-download" type="button" class="button secondary"
          :disabled="downloadBusy" @click="changeDownload(true)">{{ downloadBusy ? 'Pausing…' : 'Pause download' }}</button>
        <button v-show="!download.active && download.status !== 'completed'" id="resume-download" type="button" class="button secondary"
          :disabled="downloadBusy" @click="changeDownload(false)">{{ downloadBusy ? 'Starting…' : download.actionLabel }}</button>
      </div>
    </article>
    <div class="dataset-toolbar">
      <label class="field"><span>SHOW DATASET</span>
        <select id="records-dataset-select" v-model="dashboard.datasetFilter" :disabled="running">
          <option value="all">All datasets</option><option value="roadsens">RoadSens-4M</option><option value="comma2k19">comma2k19 · Unlabeled</option>
        </select>
      </label>
      <button id="refresh-datasets" type="button" class="button secondary" :disabled="!dashboard.ready || running || dashboard.datasetsRefreshing"
        @click="refresh">{{ dashboard.datasetsRefreshing ? 'Refreshing…' : 'Refresh recordings' }}</button>
      <span id="dataset-refresh-status" class="helper" role="status">{{ dashboard.datasetRefreshStatus }}</span>
    </div>
    <div class="records-layout">
      <article class="panel">
        <div class="panel-heading"><h2>Available recordings</h2><span id="recordings-count" class="count-chip">{{ recordings.length }}</span></div>
        <div id="recordings-list" class="recordings-list">
          <div v-if="!recordings.length" class="table-empty">{{ emptyRecordings }}</div>
          <div v-for="recording in recordings" :key="recording.id" class="recording-row">
            <span class="recording-icon">▷</span>
            <div>
              <strong>{{ recording.label || 'Recording ' + recording.id }}</strong>
              <small>{{ recordingInfo(recording) }}</small>
              <span v-if="unlabeled(recording)" class="badge provisional recording-badge">Unlabeled</span>
            </div>
            <button type="button" class="button secondary" :disabled="!recording.video_path || running"
              @click="openRecording(recording)">{{ recording.video_path ? 'Open ↗' : 'No video' }}</button>
          </div>
        </div>
      </article>
      <article class="panel import-panel">
        <div class="eyebrow">EXCEL / CSV</div><h2>Import anomaly records</h2>
        <p>Match detections to reference coordinates, hazard classes, and timestamps.</p>
        <form id="import-form" @submit.prevent="importReferences">
          <label class="upload-zone" for="reference-file">
            <svg viewBox="0 0 24 24"><path d="M12 16V3m-4 4 4-4 4 4M4 14v7h16v-7"/></svg>
            <strong id="upload-label">{{ filename || 'Choose a spreadsheet' }}</strong><span>.xlsx or .csv · All worksheets by default</span>
            <input id="reference-file" name="file" type="file" accept=".xlsx,.csv" required @change="chooseSpreadsheet">
          </label>
          <details class="mapping-details">
            <summary>Custom column mapping <span>Optional</span></summary>
            <p class="helper">Leave blank for automatic column detection. Enter exact column headers when your file uses different names.</p>
            <div class="fields-grid">
              <label v-for="[key, label, placeholder] in mappingFields" :key="key" class="field">
                <span>{{ label }}</span><input v-model.trim="mapping[key]" :data-mapping="key" :placeholder="placeholder">
              </label>
            </div>
          </details>
          <button class="button primary full-width" type="submit" :disabled="importing">{{ importing ? 'Importing…' : 'Import reference records' }}</button>
          <div id="import-result" class="helper" role="status">{{ importResult }}</div>
        </form>
      </article>
    </div>
    <div class="notice amber">
      <span class="notice-icon">i</span><div><strong>Reference location quality matters</strong><span id="reference-explanation">{{ referenceExplanation }}</span></div>
    </div>
    <article class="panel">
      <div class="panel-heading">
        <div class="panel-title"><h2>Reference anomalies</h2><span id="reference-count" class="count-chip">{{ references.length }}</span></div>
        <span id="reference-source" class="muted small">{{ recordingId ? 'Recording ' + recordingId + ' + imported records' : 'Imported records' }}</span>
      </div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>RECORD ID</th><th>HAZARD CLASS</th><th>LATITUDE</th><th>LONGITUDE</th><th>TIME / RECORDING</th><th>LOCATION SOURCE</th></tr></thead>
          <tbody id="references-body">
            <tr v-for="row in referenceRows" :key="row.id">
              <td :title="row.id">{{ row.id.slice(0, 14) }}</td><td>{{ row.hazard }}</td>
              <td class="coords">{{ row.latitude }}</td><td class="coords">{{ row.longitude }}</td><td>{{ row.timeLabel }}</td><td>{{ row.location }}</td>
            </tr>
          </tbody>
        </table>
        <div v-show="!references.length" id="references-empty" class="table-empty">
          <strong>{{ selectedUnlabeled ? 'Unlabeled recording' : 'No reference records available' }}</strong>
          <span>{{ selectedUnlabeled ? 'No anomaly labels supplied. YOLO outputs are predictions.' : 'Import an anomaly spreadsheet to compare it with detected hazards.' }}</span>
        </div>
      </div>
      <div class="panel-bottom">
        <span id="references-summary">{{ references.length }} reference records{{ recordingId ? ' · Recording ' + recordingId : '' }}{{ references.length > 500 ? ' · Showing first 500' : '' }}</span>
        <span>Matching uses class, distance, and time when available</span>
      </div>
    </article>
  </section>

`;

export default {
  name: 'DatasetRecords',
  setup() {
    const downloadBusy = ref(false);
    const importing = ref(false);
    const importResult = ref('');
    const filename = ref('');
    const mapping = reactive({});
    const running = computed(() => dashboard.state?.status === 'running');
    const recordings = computed(() => (dashboard.recordings || []).filter(recording =>
      dashboard.datasetFilter === 'all' || dashboard.datasetFilter === recordingDataset(recording)));
    const emptyRecordings = computed(() => {
      if (!dashboard.ready) return 'Loading dataset…';
      if (dashboard.datasetFilter === 'comma2k19') return 'No complete comma2k19 segments are available yet. Download progress appears above.';
      return 'No recordings found for this dataset.';
    });
    const stats = computed(() => {
      const dataset = dashboard.dataset || {};
      const allRecordings = dashboard.recordings || [];
      return [
        ['Playable recordings', dataset.playable_recording_count ?? allRecordings.filter(row => row.video_path).length, 'of ' + (dataset.recording_count ?? allRecordings.length) + ' folders'],
        ['Recorded anomaly events', dataset.reference_event_count ?? '—', 'Annotation.csv'],
        ['Excel workbooks found', dataset.workbook_count ?? 0, dataset.workbook_count ? 'in dataset' : 'Upload to add yours'],
      ];
    });
    const datasetCards = computed(() => (dashboard.datasets || []).map(dataset => {
      const missing = Number(dataset.unlabeled_recording_count) || 0;
      return { ...dataset, missing, unlabeled: dataset.has_labels === false || dataset.id === 'comma2k19'
        || (missing > 0 && missing === Number(dataset.recording_count)) };
    }));
    const download = computed(() => {
      const data = dashboard.download || {};
      const status = data.status || 'idle';
      const active = ['starting', 'downloading', 'verifying', 'extracting'].includes(status);
      const total = Number(data.bytes_total) > 0 ? Number(data.bytes_total) : null;
      const bytes = Math.max(0, Number(data.bytes_downloaded) || 0);
      return {
        status, active, total,
        label: downloadLabels[status] || status,
        progress: Math.min(bytes, total ?? bytes),
        progressLabel: formatBytes(bytes) + ' of ' + formatBytes(total),
        bytesLabel: formatBytes(bytes) + (total ? ' / ' + formatBytes(total) : ' downloaded · total size pending')
          + (active && Number(data.bytes_per_second) > 0 ? ' · ' + formatBytes(data.bytes_per_second) + '/s' : ''),
        filesLabel: (data.files_completed ?? 0) + (numeric(data.files_total) ? ' / ' + data.files_total : '') + ' files complete'
          + (numeric(data.segments_ready) ? ' · ' + data.segments_ready + ' segments ready' : ''),
        currentFile: data.current_file ? 'Current file: ' + data.current_file : '',
        message: data.message || (status === 'paused' ? 'Download is paused. Existing files will be reused when you resume.' : ''),
        actionLabel: status === 'error' ? 'Retry full download' : status === 'paused' ? 'Resume full download' : 'Download full dataset',
      };
    });

    const references = computed(() => dashboard.references || []);
    const recordingId = computed(() => dashboard.selectedRecording);
    const selectedUnlabeled = computed(() => unlabeled(dashboard.selectedRecordingDetails?.recording
      || dashboard.recordings?.find(recording => String(recording.id) === String(recordingId.value))));
    const referenceExplanation = computed(() => {
      const issues = dashboard.dataset?.issues || [];
      return issues.length
        ? issues.join(' ') + ' Dataset event positions use the vehicle GPS at annotation time.'
        : 'Dataset anomaly events use the vehicle position at the event time when available. Imported rows keep their supplied coordinates.';
    });
    const referenceRows = computed(() => references.value.slice(0, 500).map(reference => {
      const coordinates = position(reference);
      return {
        id: String(reference.id ?? '—'),
        hazard: className(reference),
        latitude: coordinates?.[0] ?? '—',
        longitude: coordinates?.[1] ?? '—',
        timeLabel: (numeric(reference.time_s) ? time(reference.time_s) + ' · ' : '')
          + (reference.recording_id ? 'Recording ' + reference.recording_id : 'Any recording'),
        location: reference.location_kind === 'vehicle_at_annotation' ? 'Vehicle-at-event proxy'
          : coordinates ? 'Supplied coordinates' : 'Time only · No GPS',
      };
    }));

    function recordingInfo(recording) {
      return [
        datasetName(recordingDataset(recording)),
        recording.video_path ? 'Video available' : 'Video missing',
        recording.sensor_path || recording.has_gps ? 'GPS track' : 'No GPS track',
        unlabeled(recording) ? 'Unlabeled · predictions only' : recording.annotation_path ? 'Anomaly annotations' : 'Labels unavailable',
      ].join(' · ');
    }

    function openRecording(recording) {
      if (running.value || !recording.video_path) return;
      const id = String(recording.id);
      dashboard.selectedRecording = id;
      dashboard.openRecordingRequest = { id };
      go('live');
    }

    async function refresh() {
      if (dashboard.datasetsRefreshing || running.value) return;
      dashboard.datasetRefreshStatus = '';
      try {
        await refreshDatasets();
      } catch (error) {
        dashboard.datasetRefreshStatus = error.message;
        notify(error.message, true);
      }
    }

    async function changeDownload(pause) {
      if (downloadBusy.value) return;
      downloadBusy.value = true;
      try {
        const route = pause ? 'stop' : 'start';
        const result = await api('/api/datasets/download/' + route, {
          method: 'POST',
          ...(pause ? {} : { body: { scope: 'full' } }),
        });
        dashboard.download = result.download || result;
      } catch (error) {
        notify(error.message, true);
      } finally {
        downloadBusy.value = false;
      }
    }

    function chooseSpreadsheet(event) {
      filename.value = event.currentTarget.files[0]?.name || '';
    }

    async function importReferences(event) {
      if (importing.value) return;
      const file = event.currentTarget.elements.file.files[0];
      if (!file) {
        notify('Choose an Excel workbook or CSV file.', true);
        return;
      }
      const data = new FormData();
      data.append('file', file);
      data.append('mapping', JSON.stringify(Object.fromEntries(Object.entries(mapping).filter(([, value]) => value))));
      importing.value = true;
      try {
        const result = await api('/api/references/import', { method: 'POST', body: data });
        const imported = result.records || [];
        importResult.value = imported.length + ' reference records imported.'
          + (result.columns?.length ? '\nColumns: ' + result.columns.join(', ') : '')
          + (result.issues?.length ? '\n' + result.issues.join('\n') : '');
        if (!recordingId.value) {
          dashboard.references = [...new Map([...references.value, ...imported].map(row => [row.id, row])).values()];
        }
        dashboard.referencesVersion += 1;
        notify(imported.length
          ? 'Reference records imported. Start a new session to use the updated matches.'
          : 'No usable records were imported. Review the column mapping and messages below.', !imported.length);
      } catch (error) {
        notify(error.message, true);
      } finally {
        importing.value = false;
      }
    }

    return { dashboard, datasetName, stats, datasetCards, download, downloadBusy, changeDownload,
      running, recordings, emptyRecordings, recordingInfo, unlabeled, openRecording,
      refresh, mappingFields, mapping, filename, chooseSpreadsheet, importing,
      importResult, importReferences, references, recordingId, selectedUnlabeled, referenceExplanation,
      referenceRows };
  },
  template: RECORDS_TEMPLATE,
};

