import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from './vendor/vue.esm-browser.prod.js';
import { api, go, requestReplay } from './shared.js';

export const finite = (value) => value != null && value !== '' && Number.isFinite(Number(value));
export const formatNumber = (value, digits = 1) => finite(value) ? Number(value).toFixed(digits) : 'Unknown';
export const hazardClass = (item) => item.class_name || item.hazard_type || 'Other';

export function coordinateText(item) {
  const { latitude, longitude, location_reference: reference } = item;
  if (!finite(latitude) || !finite(longitude) || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return 'GPS unavailable';
  const label = reference === 'capture' ? 'Camera GPS: ' : reference === 'hazard' ? 'Reported hazard GPS: ' : '';
  return label + formatNumber(latitude, 6) + ', ' + formatNumber(longitude, 6);
}

export function distanceText(item) {
  if (finite(item.distance_m)) return formatNumber(item.distance_m) + (item.provisional ? ' m (provisional)' : ' m estimated');
  return item.projection_reason ? 'Distance unavailable: ' + item.projection_reason : '';
}

const isPhoto = (file) => file && /\.(jpe?g|png|webp|bmp)$/i.test(file.name);
const isVideo = (file) => file && /\.(mp4|mov|avi|mkv|webm|m4v)$/i.test(file.name);
const finished = (job) => job.status === 'completed' || job.status === 'error';
const pendingJobKey = 'roadwatch_submission_job';

function geolocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error('Location is unavailable in this browser. Enter coordinates instead.'));
      return;
    }
    navigator.geolocation.getCurrentPosition(resolve, () => {
      reject(new Error('Could not access your location. Allow location access or enter coordinates manually.'));
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
}

function submissionData(form, file, automaticVideo, manual) {
  if (file && !isPhoto(file) && !isVideo(file)) throw new Error('Choose a supported photo or video file.');
  if (file && file.size > (isPhoto(file) ? 25 : 250) * 1024 * 1024) {
    throw new Error(isPhoto(file) ? 'Choose a photo smaller than 25 MB.' : 'Choose a video smaller than 250 MB.');
  }
  const data = new FormData(form);
  const trackText = String(data.get('gps_track')).trim();
  if (trackText) {
    let track;
    try { track = JSON.parse(trackText); }
    catch { throw new Error('The GPS track must be a valid JSON array.'); }
    if (!Array.isArray(track) || track.length < 2) {
      throw new Error('The GPS track must contain at least two fixes with time_s, latitude and longitude values.');
    }
    data.set('gps_track', trackText);
  } else {
    data.delete('gps_track');
  }
  const latitude = data.get('latitude');
  const longitude = data.get('longitude');
  if (latitude || longitude || (!trackText && !automaticVideo)) {
    if (!finite(latitude) || !finite(longitude) || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) {
      throw new Error('Enter a latitude from -90 to 90 and a longitude from -180 to 180.');
    }
  } else {
    data.delete('latitude');
    data.delete('longitude');
  }
  if (!data.get('gps_accuracy_m')) data.delete('gps_accuracy_m');
  if (!file) data.delete('file');
  if (!manual) data.delete('hazard_type');
  return data;
}

export default {
  setup() {
    const mode = ref('detect');
    const manual = computed(() => mode.value === 'manual');
    const file = shallowRef(null);
    const previewUrl = ref('');
    const useCalibration = ref(false);
    const automaticVideo = computed(() => !manual.value && Boolean(isVideo(file.value)));
    const fileLabel = computed(() => file.value?.name || (manual.value ? 'Add a photo or video · optional' : 'Choose a photo or video'));
    const latitude = ref('');
    const longitude = ref('');
    const accuracy = ref('');
    const locationReference = ref('capture');
    const locationError = ref('');
    const locating = ref(false);
    const locationNote = computed(() => {
      const note = locationReference.value === 'hazard'
        ? 'A hazard point applies to all detections unless you supply a GPS track.'
        : 'Camera coordinates give an approximate hazard location. For moving video, use a GPS track to vary the camera position.';
      return automaticVideo.value ? note + ' Leave both fields blank to detect without GPS.' : note;
    });

    const job = shallowRef(null);
    const pollError = ref('');
    const resultPanel = ref(null);
    const busy = computed(() => Boolean(job.value && !finished(job.value)));
    const progress = computed(() => Math.min(100, Math.max(0, Number(job.value?.progress) || 0)));
    const findings = computed(() => (job.value?.hazards || []).slice(0, 12));
    const jobError = computed(() => job.value?.status === 'error' || Boolean(pollError.value));
    const jobTitle = computed(() => ({
      uploading: 'Uploading your evidence…',
      queued: 'Your report is queued',
      processing: 'Detecting road hazards…',
      completed: 'Processing complete',
      error: 'Your report needs attention',
    }[job.value?.status] || 'Preparing your upload…'));
    const jobMessage = computed(() => pollError.value || job.value?.error || job.value?.message || (progress.value.toFixed(0) + '% processed. You can keep this page open while the work continues.'));
    let pollTimer;
    let disposed = false;

    function summary(item) {
      return [hazardClass(item), distanceText(item), coordinateText(item)].filter(Boolean).join(' · ');
    }

    function selectFile(event) {
      if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
      file.value = event.target.files[0] || null;
      previewUrl.value = isPhoto(file.value) || isVideo(file.value) ? URL.createObjectURL(file.value) : '';
      useCalibration.value = false;
    }

    async function fillLocation() {
      locating.value = true;
      locationError.value = '';
      try {
        const position = await geolocation();
        latitude.value = position.coords.latitude;
        longitude.value = position.coords.longitude;
        accuracy.value = Math.round(position.coords.accuracy * 10) / 10;
        locationReference.value = 'capture';
      } catch (error) {
        locationError.value = error.message;
      } finally {
        locating.value = false;
      }
    }

    function updateJob(result) {
      job.value = result;
      pollError.value = '';
      if (finished(result)) sessionStorage.removeItem(pendingJobKey);
    }

    async function pollJob(id) {
      if (disposed) return;
      try {
        updateJob(await api('/api/submissions/' + encodeURIComponent(id)));
        if (!disposed && !finished(job.value)) pollTimer = setTimeout(() => pollJob(id), 1500);
      } catch (error) {
        if (disposed) return;
        pollError.value = 'Could not read progress: ' + error.message + ' Retrying…';
        pollTimer = setTimeout(() => pollJob(id), 5000);
      }
    }

    async function submitHazard(event) {
      if (busy.value) return;
      try {
        const data = submissionData(event.currentTarget, file.value, automaticVideo.value, manual.value);
        updateJob({ status: 'uploading', progress: 0, message: 'Uploading to your local RoadWatch server…' });
        await nextTick();
        resultPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const result = await api('/api/submissions', { method: 'POST', body: data });
        updateJob(result);
        if (result.id && !finished(result)) {
          sessionStorage.setItem(pendingJobKey, result.id);
          pollTimer = setTimeout(() => pollJob(result.id), 800);
        }
      } catch (error) {
        updateJob({ status: 'error', progress: 0, error: error.message });
      }
    }

    function openReplay() {
      if (job.value?.session_id) requestReplay('session:' + job.value.session_id);
    }

    onMounted(() => {
      const pending = sessionStorage.getItem(pendingJobKey);
      if (pending) {
        updateJob({ status: 'processing', progress: 0 });
        pollJob(pending);
      }
    });
    onBeforeUnmount(() => {
      disposed = true;
      clearTimeout(pollTimer);
      if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
    });

    return {
      go, mode, manual, file, isVideo, previewUrl, useCalibration, automaticVideo, fileLabel, selectFile,
      latitude, longitude, accuracy, locationReference, locationError, locating, locationNote, fillLocation,
      job, resultPanel, busy, progress, findings, jobError, jobTitle, jobMessage, summary, submitHazard, openReplay,
    };
  },
  template: `
    <section id="tab-submit" class="tab-panel" aria-labelledby="submit-title">
      <div class="page-heading">
        <div>
          <div class="eyebrow">SEE IT. RECORD IT. REPORT IT.</div>
          <h1 id="submit-title">Report a hazard</h1>
          <p>Upload a photo or video for YOLO detection, or enter a hazard manually.</p>
        </div>
      </div>
      <div class="submission-layout">
        <article class="panel submission-form-panel">
          <form id="submission-form" @submit.prevent="submitHazard">
            <fieldset class="submission-mode">
              <legend>How would you like to log it?</legend>
              <label>
                <input type="radio" name="mode" value="detect" v-model="mode">
                <span>
                  <strong>Detect from a photo or video</strong>
                  <small>Your YOLO model finds supported road hazards.</small>
                </span>
              </label>
              <label>
                <input type="radio" name="mode" value="manual" v-model="mode">
                <span>
                  <strong>Log a hazard manually</strong>
                  <small>Describe what you saw; a photo is optional.</small>
                </span>
              </label>
            </fieldset>
            <label class="upload-zone submission-upload" for="submission-file">
              <svg viewBox="0 0 24 24">
                <path d="M12 16V3m-4 4 4-4 4 4M4 14v7h16v-7"/>
              </svg>
              <strong id="submission-file-label">{{ fileLabel }}</strong>
              <span>Photo up to 25 MB · Video up to 250 MB / 5 minutes</span>
              <input
                id="submission-file" name="file" type="file"
                accept=".jpg,.jpeg,.png,.webp,.bmp,.mp4,.mov,.avi,.mkv,.webm,.m4v" :required="!manual"
                @change="selectFile">
            </label>
            <div id="submission-preview" class="submission-preview" :hidden="!previewUrl">
              <video v-if="previewUrl && isVideo(file)" :src="previewUrl" controls preload="metadata"></video>
              <img v-else-if="previewUrl" :src="previewUrl" alt="Selected hazard photo preview">
            </div>
            <section
              id="submission-calibration" class="submission-calibration"
              aria-labelledby="submission-calibration-title" :hidden="!automaticVideo">
              <div class="submission-section-heading">
                <h2 id="submission-calibration-title">Camera calibration</h2>
                <button type="button" id="configure-upload-camera" class="text-button" @click="go('calibration', true)">Configure camera</button>
              </div>
              <label class="checkbox-label">
                <input
                  id="submission-use-calibration" type="checkbox" name="use_calibration" value="true"
                  :disabled="!automaticVideo" v-model="useCalibration">
                <span>
                  <strong>Apply saved camera calibration</strong>
                  <small>Estimate ground distance using the lens and mount settings for this video.</small>
                </span>
              </label>
            </section>
            <label id="manual-class-field" class="field" :hidden="!manual">
              <span>HAZARD TYPE</span>
              <input
                name="hazard_type" :required="manual" list="manual-hazard-types" maxlength="120"
                placeholder="e.g. Pothole, broken pavement, exposed drain">
              <datalist id="manual-hazard-types">
                <option value="Pothole"></option>
                <option value="Longitudinal crack"></option>
                <option value="Transverse crack"></option>
                <option value="Alligator crack"></option>
                <option value="Exposed drain"></option>
                <option value="Other"></option>
              </datalist>
            </label>
            <div class="submission-section-heading">
              <h2 id="submission-location-title">{{ automaticVideo ? 'GPS coordinates (optional)' : 'GPS coordinates' }}</h2>
              <button
                type="button" id="submission-location" class="text-button" :disabled="locating" @click="fillLocation">
                {{ locating ? 'Locating…' : 'Use my location ⌖' }}
              </button>
            </div>
            <div class="fields-grid">
              <label class="field">
                <span>LATITUDE</span>
                <input
                  name="latitude" id="submission-latitude" v-model="latitude" type="number" min="-90" max="90"
                  step="any" placeholder="e.g. 24.3757">
              </label>
              <label class="field">
                <span>LONGITUDE</span>
                <input
                  name="longitude" id="submission-longitude" v-model="longitude" type="number" min="-180" max="180"
                  step="any" placeholder="e.g. 88.6226">
              </label>
              <label class="field span-2">
                <span>THESE COORDINATES REPRESENT</span>
                <select
                  name="location_reference" id="submission-location-reference" v-model="locationReference"
                  @change="locationError = ''">
                  <option value="capture">Where the camera / vehicle was · approximate hazard location</option>
                  <option value="hazard">The hazard's location · I selected / measured this point</option>
                </select>
              </label>
              <label class="field">
                <span>REPORTED GPS ACCURACY · M · OPTIONAL</span>
                <input
                  name="gps_accuracy_m" id="submission-accuracy" v-model="accuracy" type="number" min="0" max="10000"
                  step="any" placeholder="Leave blank if unknown">
              </label>
              <label class="field">
                <span>PRIORITY</span>
                <select name="severity">
                  <option value="medium">Medium</option>
                  <option value="low">Low</option>
                  <option value="high">High</option>
                </select>
              </label>
            </div>
            <p id="submission-location-note" class="helper" :class="{ 'video-error': locationError }">{{ locationError || locationNote }}</p>
            <details class="submission-gps-details">
              <summary>
                Moving video: add a synchronized GPS track
                <span>Optional</span>
              </summary>
              <p class="helper">Paste a JSON array with seconds from the start of the video and latitude/longitude. Positions outside the track or across large gaps are left unavailable.</p>
              <label class="field">
                <span>GPS TRACK JSON</span>
                <textarea
                  name="gps_track" id="submission-gps-track" rows="4"
                  placeholder='[{"time_s":0,"latitude":24.3757,"longitude":88.6226},{"time_s":1,"latitude":24.3758,"longitude":88.6227}]'></textarea>
              </label>
            </details>
            <div class="submission-section-heading">
              <h2>Add helpful details</h2>
              <span class="muted small">Optional</span>
            </div>
            <div class="fields-grid">
              <label class="field span-2">
                <span>REPORT TITLE</span>
                <input name="title" maxlength="240" placeholder="e.g. Deep pothole near the school entrance">
              </label>
              <label class="field">
                <span>STREET / LANDMARK / WARD</span>
                <input name="address" maxlength="500" placeholder="Street or nearby landmark">
              </label>
              <label class="field span-2">
                <span>WHAT DID YOU OBSERVE?</span>
                <textarea
                  name="notes" maxlength="4000" rows="3"
                  placeholder="Describe the condition and how it affects road users."></textarea>
              </label>
            </div>
            <button id="submission-submit" type="submit" class="button primary full-width" :disabled="busy">{{ manual ? 'Save manual hazard' : 'Detect and save hazards' }}</button>
            <p id="submission-local-note" class="helper">Uploads and evidence are stored locally. YOLO results are predictions for your review.</p>
          </form>
        </article>
        <aside class="submission-guide">
          <article class="panel submission-guide-card">
            <span class="section-number">01</span>
            <h2>Clear evidence</h2>
            <p>Use a sharp road photo or a short video. New detections save a maximum of 10 evidence frames for each tracked hazard.</p>
          </article>
          <article class="panel submission-guide-card">
            <span class="section-number">02</span>
            <h2>Honest location</h2>
            <p>Choose whether your coordinates identify the hazard or the camera. Camera locations and model estimates stay marked as approximate.</p>
          </article>
          <article
            id="submission-result" class="panel submission-result" aria-live="polite" :hidden="!job" ref="resultPanel">
            <span class="eyebrow">SUBMISSION STATUS</span>
            <h2 id="submission-result-title">{{ jobTitle }}</h2>
            <progress id="submission-progress" max="100" :value="progress" aria-label="Submission processing progress"></progress>
            <p id="submission-result-message" :class="{ 'video-error': jobError }">{{ jobMessage }}</p>
            <div id="submission-results-list">
              <div v-for="hazard in findings" :key="hazard.id" class="submission-result-item">{{ summary(hazard) }}</div>
            </div>
            <button
              id="submission-open-replay" type="button" class="button secondary"
              :hidden="job?.status !== 'completed' || job?.media_kind !== 'video'" @click="openReplay">
              Open video in replay
            </button>
          </article>
        </aside>
      </div>
    </section>
  `,
};
