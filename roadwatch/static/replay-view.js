import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from './vendor/vue.esm-browser.prod.js';
import { api, dashboard, go } from './shared.js';
import { coordinateText, distanceText, finite, formatNumber, hazardClass } from './submit.js';
import { HazardReportActions } from './hazard-report.js';

const frameIntervalMs = 200;
const initialStatus = 'Detection runs as you play or seek. Saved frames are reused when revisited.';
const safeMedia = (url) => typeof url === 'string' && url.startsWith('/api/');
const hazardTime = (item) => Number(item.first_video_time_s ?? item.video_time_s ?? item.first_seen_s) || 0;
const confidenceText = (item) => finite(item.confidence) ? formatNumber(Number(item.confidence) * 100, 0) + '%' : '';

function clock(value) {
  const total = Math.max(0, Number(value) || 0);
  const minutes = Math.floor(total / 60);
  const seconds = Math.floor(total % 60);
  return String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
}

function hazardTitle(hazard) {
  return hazardClass(hazard) + ' at ' + clock(hazardTime(hazard));
}

function detectionText(item) {
  const distance = finite(item.distance_m) ? distanceText(item) : '';
  const track = item.track_id == null ? '' : '#' + item.track_id;
  return [confidenceText(item), distance, track].filter(Boolean).join(' · ');
}

export default {
  components: { HazardReportActions },
  setup() {
    const sources = shallowRef([]);
    const source = ref('');
    const query = ref('');
    const loadingSources = ref(false);
    const visibleSources = computed(() => {
      const search = query.value.trim().toLowerCase();
      return sources.value.filter((video) => !search || video.id === source.value
        || ((video.label || '') + ' ' + video.id).toLowerCase().includes(search));
    });
    const sourcePrompt = computed(() => {
      if (loadingSources.value && !sources.value.length) return 'Loading available videos…';
      return visibleSources.value.length ? 'Choose a video to detect…' : 'No videos match this search';
    });
    const info = shallowRef(null);
    const sourceMeta = computed(() => {
      if (!info.value) return '';
      const { width, height, fps, duration_s: duration } = info.value;
      return [width + ' × ' + height, formatNumber(fps) + ' source fps', clock(duration), 'YOLO enabled'].join(' · ');
    });
    const time = ref(0);
    const speed = ref(1);
    const playing = ref(false);
    const playText = computed(() => {
      if (playing.value) return 'Pause';
      return info.value && time.value >= info.value.duration_s ? 'Replay & detect' : 'Play & detect';
    });
    const pending = ref(false);
    const processingText = ref('Analyzing frame…');
    const imageUrl = ref('');
    const frameMeta = ref('Choose a video');
    const detector = shallowRef({ text: 'Ready to detect', kind: 'idle' });
    const status = shallowRef({ text: initialStatus, error: false });
    const detections = shallowRef([]);
    const hazards = shallowRef([]);
    const orderedHazards = computed(() => [...hazards.value].sort((left, right) => hazardTime(left) - hazardTime(right)));
    const evidenceCount = ref(0);

    let sourcesTask;
    let analysisId;
    let version = 0;
    let frameSequence = 0;
    let playSequence = 0;
    let requestSequence = 0;
    let playTimer;
    let seekTimer;
    let analysisQueue = Promise.resolve();

    function detectorState(text, kind = 'idle') {
      detector.value = { text, kind };
    }

    function message(text, error = false) {
      status.value = { text, error };
    }

    function updateFindings(result) {
      hazards.value = result.hazards || [];
      evidenceCount.value = finite(result.evidence_count) ? Number(result.evidence_count)
        : hazards.value.reduce((sum, item) => sum + (Number(item.evidence_count) || 0), 0);
    }

    function tickPosition(hazard) {
      const duration = Number(info.value?.duration_s) || 1;
      return Math.min(99, Math.max(0, hazardTime(hazard) / duration * 100)) + '%';
    }

    async function loadSources() {
      if (sourcesTask) return sourcesTask;
      loadingSources.value = true;
      sourcesTask = api('/api/replay/sources').then((result) => {
        sources.value = (result.sources || []).filter((video) => video.available !== false);
        return sources.value;
      }).catch((error) => {
        message('Could not load videos: ' + error.message + ' Use Refresh videos to retry.', true);
        return null;
      }).finally(() => {
        sourcesTask = null;
        loadingSources.value = false;
      });
      return sourcesTask;
    }

    async function refreshSources() {
      const available = await loadSources();
      if (available && source.value && !available.some((video) => video.id === source.value)) {
        source.value = '';
        await loadSource();
      }
    }

    function pause() {
      playing.value = false;
      playSequence += 1;
      clearTimeout(playTimer);
      if (analysisId && !pending.value) detectorState('Paused · detections saved', 'paused');
    }

    // Finish queued frames before closing their analysis, and close old analysis
    // before starting another. The backend grants one inference lease at a time.
    function withAnalysis(action) {
      const task = analysisQueue.then(action);
      analysisQueue = task.catch(() => {});
      return task;
    }

    async function closeAnalysis(id) {
      try {
        await api('/api/replay/close', { method: 'POST', body: { analysis_id: id } });
      } catch {
        // The server releases an idle analysis lease if the close fails.
      }
    }

    function releaseAnalysis() {
      const id = analysisId;
      analysisId = null;
      if (id) return withAnalysis(() => closeAnalysis(id));
    }

    function current(expectedVersion) {
      return expectedVersion === version && dashboard.tab === 'replay';
    }

    async function ensureAnalysis(expectedVersion) {
      if (analysisId) return true;
      detectorState('Preparing YOLO detection…', 'processing');
      const result = await api('/api/replay/analyze', { method: 'POST', body: { source_id: source.value } });
      if (!current(expectedVersion)) {
        if (result.analysis_id) await closeAnalysis(result.analysis_id);
        return false;
      }
      if (!result.analysis_id || !Number.isFinite(Number(result.info?.duration_s)) || Number(result.info.duration_s) < 0) {
        if (result.analysis_id) await closeAnalysis(result.analysis_id);
        throw new Error('This video does not have a readable timeline.');
      }
      analysisId = result.analysis_id;
      info.value = result.info;
      updateFindings(result);
      return true;
    }

    function frameTime(value) {
      return Math.min(Math.max(0, Number(value) || 0), Math.max(0, Number(info.value?.duration_s) - 0.05));
    }

    async function loadSource(startAt = 0) {
      pause();
      clearTimeout(seekTimer);
      frameSequence += 1;
      const expectedVersion = ++version;
      releaseAnalysis();
      info.value = null;
      time.value = 0;
      imageUrl.value = '';
      frameMeta.value = 'Choose a video';
      detections.value = [];
      updateFindings({});
      pending.value = false;
      if (!source.value) {
        detectorState('Ready to detect');
        message(initialStatus);
        return;
      }

      pending.value = true;
      processingText.value = 'Loading YOLO and preparing your video…';
      message('Preparing detection. The first frame can take a moment while your model loads.');
      try {
        const ready = await withAnalysis(() => current(expectedVersion) && ensureAnalysis(expectedVersion));
        if (ready && current(expectedVersion)) await showFrame(frameTime(startAt));
      } catch (error) {
        if (current(expectedVersion)) {
          detectorState('Detection unavailable', 'error');
          message('Could not start detection replay: ' + error.message, true);
        }
      } finally {
        if (current(expectedVersion)) pending.value = false;
      }
    }

    function showFrame(target) {
      const expectedVersion = version;
      const sequence = ++frameSequence;
      const isLatest = () => current(expectedVersion) && sequence === frameSequence;
      return withAnalysis(async () => {
        if (!isLatest()) return false;
        pending.value = true;
        processingText.value = 'Detecting & saving evidence…';
        detectorState('YOLO detection & logging', 'processing');
        try {
          if (!await ensureAnalysis(expectedVersion) || !isLatest()) return false;
          const started = performance.now();
          const result = await api('/api/replay/process', {
            method: 'POST', body: { analysis_id: analysisId, time_s: target },
          });
          if (!isLatest()) return false;
          if (!/^data:image\/jpeg;base64,/.test(result.image_url || '') && !safeMedia(result.image_url)) {
            throw new Error('The annotated detection frame was not returned.');
          }
          imageUrl.value = result.image_url;
          if (finite(result.time_s)) time.value = Number(result.time_s);
          frameMeta.value = 'Frame ' + (result.frame_index ?? '—') + ' · ' + clock(result.time_s)
            + (result.cached ? ' · saved analysis' : ' · ' + Math.round(performance.now() - started) + ' ms');
          detections.value = result.detections || [];
          updateFindings(result);
          if (result.cached) {
            detectorState('Saved detection · no duplicate logs', 'cached');
            message('Reusing this annotated frame and its saved detections. Revisiting a frame does not duplicate hazard records.');
          } else {
            detectorState(playing.value ? 'YOLO active · evidence logging' : 'Frame analyzed · evidence saved', 'active');
            const count = detections.value.length;
            message(count + (count === 1 ? ' detection' : ' detections') + ' in this frame · bounding boxes and evidence saved automatically. Playback adapts to inference speed.');
          }
          return true;
        } catch (error) {
          if (isLatest()) {
            pause();
            detectorState('Detection paused', 'error');
            message(error.message, true);
          }
          return false;
        } finally {
          if (isLatest()) pending.value = false;
        }
      });
    }

    function seek(target) {
      if (!info.value) return;
      pause();
      clearTimeout(seekTimer);
      time.value = frameTime(target);
      return showFrame(time.value);
    }

    function scrub(event) {
      pause();
      frameSequence += 1;
      const target = Number(event.target.value);
      time.value = target;
      clearTimeout(seekTimer);
      seekTimer = setTimeout(() => seek(target), 150);
    }

    async function playFrame(sequence) {
      if (!playing.value || sequence !== playSequence || !info.value) return;
      const started = performance.now();
      const duration = Number(info.value.duration_s);
      const next = Math.min(duration, time.value + frameIntervalMs / 1000 * speed.value);
      const ok = await showFrame(frameTime(next));
      if (!ok || sequence !== playSequence) return;
      if (next >= duration || time.value >= duration - 0.06) {
        time.value = duration;
        pause();
        detectorState('Replay complete · evidence saved', 'cached');
        message('Replay complete. ' + hazards.value.length + ' hazards logged with their photo evidence.');
        releaseAnalysis();
      } else {
        playTimer = setTimeout(() => playFrame(sequence), Math.max(0, frameIntervalMs - (performance.now() - started)));
      }
    }

    function togglePlay() {
      if (playing.value) {
        pause();
        return;
      }
      if (!info.value) return;
      clearTimeout(seekTimer);
      if (time.value >= info.value.duration_s) time.value = 0;
      playing.value = true;
      playFrame(++playSequence);
    }

    function chooseSource() {
      requestSequence += 1;
      loadSource();
    }

    async function openRequested(request) {
      if (!request) return;
      const sequence = ++requestSequence;
      const available = await loadSources();
      if (!available || sequence !== requestSequence || dashboard.tab !== 'replay') return;
      const chosen = available.find((video) => video.id === request.source)
        || available.find((video) => video.id === request.fallback);
      if (!chosen) {
        message('This session has no saved source video.', true);
        return;
      }
      query.value = '';
      source.value = chosen.id;
      await loadSource(request.startAt);
    }

    function leaveReplay() {
      pause();
      clearTimeout(seekTimer);
      version += 1;
      frameSequence += 1;
      requestSequence += 1;
      pending.value = false;
      releaseAnalysis();
    }

    function closeOnUnload() {
      if (!analysisId) return;
      fetch('/api/replay/close', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ analysis_id: analysisId }), keepalive: true,
      }).catch(() => {});
    }

    watch(() => dashboard.tab, (tab, previous) => {
      if (tab === 'replay') refreshSources();
      if (previous === 'replay' && tab !== 'replay') leaveReplay();
    }, { immediate: true });
    watch(() => dashboard.replayRequest, openRequested);
    onMounted(() => window.addEventListener('beforeunload', closeOnUnload));
    onBeforeUnmount(() => {
      leaveReplay();
      window.removeEventListener('beforeunload', closeOnUnload);
    });

    return {
      go, source, query, loadingSources, visibleSources, sourcePrompt, sourceMeta, refreshSources, chooseSource,
      info, time, speed, playText, pending, processingText, imageUrl, frameMeta, detector, status, togglePlay, seek, scrub,
      detections, hazards, orderedHazards, evidenceCount, tickPosition, hazardTime, hazardTitle, clock,
      safeMedia, hazardClass, confidenceText, detectionText, coordinateText, distanceText,
    };
  },
  template: `
    <section id="tab-replay" class="tab-panel" aria-labelledby="replay-title">
      <div class="page-heading">
        <div>
          <div class="eyebrow">REPLAY. DETECT. KEEP THE EVIDENCE.</div>
          <h1 id="replay-title">Detection replay</h1>
          <p>Replay road footage with YOLO bounding boxes and automatically save each hazard's evidence.</p>
        </div>
        <div class="replay-heading-actions">
          <button type="button" class="button secondary" data-go="submit" @click="go('submit')">Upload a video</button>
          <button
            id="replay-refresh" type="button" class="button secondary" :disabled="loadingSources"
            @click="refreshSources">
            {{ loadingSources ? 'Refreshing…' : 'Refresh videos' }}
          </button>
        </div>
      </div>
      <div class="replay-workspace">
        <article class="panel replay-panel">
          <div class="replay-source-search">
            <label class="field">
              <span>FIND YOUR VIDEO</span>
              <input
                id="replay-source-search" v-model="query" type="search"
                placeholder="Search recording, date, route or uploaded filename">
            </label>
            <span id="replay-source-search-status" class="helper">{{ visibleSources.length }} videos available</span>
          </div>
          <div class="replay-source-bar">
            <label class="field">
              <span>VIDEO TO ANALYZE</span>
              <select id="replay-source" v-model="source" @change="chooseSource">
                <option value="">{{ sourcePrompt }}</option>
                <option v-for="video in visibleSources" :key="video.id" :value="video.id">{{ video.label || video.id }}</option>
              </select>
            </label>
            <span id="replay-source-meta" class="helper">{{ sourceMeta }}</span>
          </div>
          <div class="replay-video-heading">
            <span id="replay-detector-status" class="detector-state" :data-state="detector.kind">
              <i></i>
              {{ detector.text }}
            </span>
            <span id="replay-frame-meta">{{ frameMeta }}</span>
          </div>
          <div class="replay-stage">
            <img
              id="replay-frame" alt="Video frame annotated by YOLO with detected hazard bounding boxes"
              :src="imageUrl || null" :hidden="!imageUrl">
            <div id="replay-empty" class="replay-empty" :hidden="Boolean(imageUrl)">
              <span class="empty-symbol">▷</span>
              <h2>Watch the road. Keep every finding.</h2>
              <p>
                Choose a video. YOLO draws bounding boxes as it plays,
                <br>
                then stores up to 10 annotated frames for each hazard.
              </p>
            </div>
            <div id="replay-processing" class="replay-processing" :hidden="!pending">
              <span class="processing-spinner"></span>
              <strong id="replay-processing-text">{{ processingText }}</strong>
            </div>
          </div>
          <div class="replay-timeline">
            <div
              id="replay-hazard-ticks" class="replay-hazard-ticks"
              aria-label="Detected hazard positions on the video timeline">
              <button
                v-for="hazard in orderedHazards" :key="hazard.id" type="button"
                :style="{ left: tickPosition(hazard) }" :title="hazardTitle(hazard)"
                :aria-label="'Jump to ' + hazardTitle(hazard)" @click="seek(hazardTime(hazard))"></button>
            </div>
            <div class="replay-controls">
              <button id="replay-play" type="button" class="button primary" :disabled="!info" @click="togglePlay">{{ playText }}</button>
              <button id="replay-restart" type="button" class="button secondary" :disabled="!info" @click="seek(0)">↶ Start</button>
              <label class="sr-only" for="replay-seek">Replay position in seconds</label>
              <input
                id="replay-seek" type="range" min="0" :max="info ? Math.max(0.05, info.duration_s) : 1" :value="time"
                step="0.05" :disabled="!info" @input="scrub">
              <span id="replay-clock" class="replay-clock">{{ clock(time) }} / {{ clock(info?.duration_s) }}</span>
              <label class="field replay-speed">
                <span>SPEED</span>
                <select id="replay-speed" v-model.number="speed">
                  <option value="0.5">0.5×</option>
                  <option value="1">1×</option>
                  <option value="2">2×</option>
                </select>
              </label>
            </div>
          </div>
          <div class="replay-current-frame">
            <span>IN THIS FRAME</span>
            <div id="replay-detection-chips">
              <span
                v-for="(detection, index) in detections" :key="detection.track_id ?? index"
                class="replay-detection-chip" :title="detection.projection_reason || null">
                <i></i>
                <strong>{{ hazardClass(detection) }}</strong>
                <span>{{ detectionText(detection) }}</span>
              </span>
              <span v-if="!detections.length" class="replay-clear-frame">{{ imageUrl ? 'No supported anomalies detected in this frame' : 'Waiting for frame analysis…' }}</span>
            </div>
          </div>
          <p id="replay-status" class="helper replay-status" role="status" :class="{ 'video-error': status.error }">{{ status.text }}</p>
        </article>
        <aside class="replay-findings panel">
          <div class="replay-findings-heading">
            <div>
              <div class="eyebrow">AUTOMATICALLY LOGGED</div>
              <h2>Hazards in this replay</h2>
            </div>
            <span id="replay-findings-count" class="count-chip">{{ hazards.length }}</span>
          </div>
          <div class="replay-log-stats">
            <div>
              <strong id="replay-hazard-total">{{ hazards.length }}</strong>
              <span>unique hazards</span>
            </div>
            <div>
              <strong id="replay-evidence-total">{{ evidenceCount }}</strong>
              <span>saved photos</span>
            </div>
          </div>
          <p class="replay-log-note">Up to 10 annotated frames per hazard. Select a finding to jump to its video timestamp.</p>
          <div id="replay-hazard-log" class="replay-hazard-log">
            <div v-if="!hazards.length" class="replay-log-empty">
              <span>⌖</span>
              <strong>Your findings will appear here</strong>
              <p>Play or seek through the video to detect and log road hazards.</p>
            </div>
            <article v-for="hazard in orderedHazards" :key="hazard.id" class="replay-finding">
              <button type="button" class="replay-finding-jump" @click="seek(hazardTime(hazard))">
                <img
                  v-if="safeMedia(hazard.thumbnail_url)" :src="hazard.thumbnail_url"
                  :alt="hazardClass(hazard) + ' saved evidence'">
                <div>
                  <div class="replay-finding-title">
                    <strong>{{ hazardClass(hazard) }}</strong>
                    <span>{{ confidenceText(hazard) }}</span>
                  </div>
                  <span class="replay-finding-time">{{ clock(hazardTime(hazard)) }} · #{{ hazard.track_id ?? String(hazard.id).split(':').pop() }}</span>
                  <span class="replay-finding-coordinates">{{ coordinateText(hazard) }}</span>
                  <span class="replay-finding-photos">{{ Math.min(10, Number(hazard.evidence_count) || 0) }} / 10 photos saved</span>
                  <span v-if="distanceText(hazard)" class="replay-finding-coordinates">{{ distanceText(hazard) }}</span>
                </div>
              </button>
              <hazard-report-actions :hazard-id="hazard.id"></hazard-report-actions>
            </article>
          </div>
        </aside>
      </div>
    </section>
  `,
};
