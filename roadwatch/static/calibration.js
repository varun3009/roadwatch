import { computed, reactive, ref, watch } from './vendor/vue.esm-browser.prod.js';
import { api, dashboard, go, notify } from './shared.js';

const groups = [
  {
    title: 'Camera intrinsics',
    description: 'Use values from a checkerboard calibration or a measured lens model.',
    fields: [
      { path: 'calibration.width', label: 'IMAGE WIDTH · PX', min: 1 },
      { path: 'calibration.height', label: 'IMAGE HEIGHT · PX', min: 1 },
      { path: 'calibration.fx', label: 'FOCAL LENGTH FX · PX', min: 1, step: 'any' },
      { path: 'calibration.fy', label: 'FOCAL LENGTH FY · PX', min: 1, step: 'any' },
      { path: 'calibration.cx', label: 'PRINCIPAL POINT CX · PX', step: 'any' },
      { path: 'calibration.cy', label: 'PRINCIPAL POINT CY · PX', step: 'any' },
    ],
  },
  {
    title: 'Mounting geometry',
    description: 'Camera location relative to the GNSS antenna, in the vehicle frame.',
    fields: [
      { path: 'calibration.height_m', label: 'HEIGHT ABOVE ROAD · M', min: 0.05, step: 'any' },
      { path: 'calibration.pitch_deg', label: 'DOWNWARD PITCH · °', min: -89, max: 89, step: 'any', help: 'Positive points down.' },
      { path: 'calibration.yaw_deg', label: 'YAW TO VEHICLE · °', min: -180, max: 180, step: 'any', help: 'Positive turns right.' },
      { path: 'calibration.roll_deg', label: 'CAMERA ROLL · °', min: -180, max: 180, step: 'any', help: 'Positive rotates clockwise.' },
      { path: 'calibration.offset_forward_m', label: 'CAMERA FORWARD OFFSET · M', step: 'any', help: 'From GNSS antenna; negative is behind.' },
      { path: 'calibration.offset_right_m', label: 'CAMERA RIGHT OFFSET · M', step: 'any', help: 'From GNSS antenna; negative is left.' },
    ],
  },
  {
    title: 'Detection & matching',
    description: 'Load your trained YOLO model and tune inference for your hardware.',
    fields: [
      { path: 'detection.model_path', label: 'YOLO MODEL PATH', text: true, wide: true, placeholder: 'best.pt' },
      { path: 'detection.confidence', label: 'CONFIDENCE THRESHOLD', min: 0.01, max: 1, step: 0.01 },
      { path: 'detection.imgsz', label: 'INFERENCE IMAGE SIZE · PX', min: 32, step: 32 },
      { path: 'detection.device', label: 'COMPUTE DEVICE', options: { cpu: 'CPU', auto: 'Auto', '0': 'GPU 0' } },
      { path: 'detection.frame_stride', label: 'PROCESS EVERY NTH FRAME', min: 1, max: 60 },
      { path: 'detection.match_radius_m', label: 'RECORD MATCH RADIUS · M', min: 0.1, step: 'any' },
      { path: 'detection.time_tolerance_s', label: 'TIME TOLERANCE · S', min: 0, step: 'any' },
    ],
  },
  {
    title: 'Projection & GPS quality',
    description: 'Reject unstable rays near the horizon and stale GPS fixes.',
    fields: [
      { path: 'calibration.max_distance_m', label: 'MAXIMUM RANGE · M', min: 1, step: 'any' },
      { path: 'calibration.min_downward_angle_deg', label: 'MIN. DOWNWARD RAY ANGLE · °', min: 0.1, max: 45, step: 'any' },
      { path: 'calibration.point_mode', label: 'DETECTION GROUND POINT', options: { bottom_center: 'Bounding box bottom center', center: 'Bounding box center' }, help: 'Choose the point that touches the road.' },
      { path: 'pose.max_age_s', label: 'GPS MAXIMUM AGE · S', min: 0.1, step: 'any' },
      { path: 'pose.accuracy_m', label: 'GPS DEFAULT ACCURACY · M', min: 0, step: 'any' },
      { path: 'sync_offset_s', label: 'VIDEO / GPS SYNC OFFSET · S', step: 'any', help: 'Added to video time when looking up GPS.' },
    ],
  },
];

const paths = [...groups.flatMap(group => group.fields.map(field => field.path)), 'calibration.verified'];

const CALIBRATION_TEMPLATE = `
  <section id="tab-calibration" class="tab-panel" aria-labelledby="calibration-title">
    <div class="page-heading">
      <div><div class="eyebrow">A MEASURED FOUNDATION</div><h1 id="calibration-title">Camera calibration</h1>
        <p>Make the camera model fit your vehicle, lens, and mounting position.</p>
      </div>
      <button type="submit" form="config-form" class="button primary" :disabled="saving || !dashboard.config">
        {{ saving ? 'Saving…' : 'Save configuration' }}
      </button>
    </div>
    <button v-show="dashboard.fromVideoUpload" type="button" id="upload-calibration-return" class="text-button" @click="go('submit')">
      Back to video upload
    </button>
    <div class="notice blue"><span class="notice-icon">i</span><div><strong>Distance uses a road-plane projection</strong>
      <span>Measure mounting height and orientation carefully. Hills, deep potholes, camera movement, and lens errors affect accuracy.</span>
    </div></div>
    <div v-show="showDatasetCalibration" id="dataset-calibration-notice" class="notice amber">
      <span class="notice-icon">i</span><div><strong>comma2k19 uses its published camera intrinsics</strong>
        <span id="dataset-calibration-detail">{{ datasetExplanation }}</span>
      </div>
    </div>
    <form id="config-form" @submit.prevent="save">
      <div class="settings-grid">
        <article v-for="(group, index) in groups" :key="group.title" class="panel settings-panel">
          <div class="section-number">0{{ index + 1 }}</div><h2>{{ group.title }}</h2><p>{{ group.description }}</p>
          <div class="fields-grid">
            <label v-for="field in group.fields" :key="field.path" class="field" :class="{ 'span-2': field.wide }">
              <span>{{ field.label }}</span>
              <select v-if="field.options" v-model="draft[field.path]" :data-config="field.path">
                <option v-for="(text, value) in field.options" :key="value" :value="value">{{ text }}</option>
              </select>
              <input v-else-if="field.text" v-model.trim="draft[field.path]" :data-config="field.path"
                type="text" :placeholder="field.placeholder" required>
              <input v-else v-model.number="draft[field.path]" :data-config="field.path" type="number"
                :min="field.min" :max="field.max" :step="field.step" required>
              <small v-if="field.help">{{ field.help }}</small>
            </label>
            <label v-if="index === 0" class="field span-2"><span>DISTORTION · K1, K2, P1, P2, K3</span>
              <input type="text" id="distortion-input" v-model="distortion" placeholder="0, 0, 0, 0, 0">
              <small>OpenCV coefficient order; comma-separated numbers.</small>
            </label>
          </div>
        </article>
      </div>
      <div class="panel verify-panel"><div><label class="checkbox-label">
        <input id="calibration-verified" type="checkbox" data-config="calibration.verified" v-model="draft['calibration.verified']">
        <span><strong>I have entered and verified measured camera calibration.</strong>
          <small>This acknowledgement marks projections as calibrated. It does not independently validate measurement accuracy.</small>
        </span>
      </label></div>
        <button class="button primary" type="submit" :disabled="saving || !dashboard.config">{{ saving ? 'Saving…' : 'Save configuration' }}</button>
      </div>
      <p class="helper">Stop detection before saving changes. Start a new session to apply the updated camera and inference settings.</p>
    </form>
    <article class="panel checkerboard-panel">
      <div><div class="eyebrow">OPTIONAL · LENS CALIBRATION</div><h2>Calibrate with a checkerboard</h2>
        <p>Upload multiple sharp views of the same flat checkerboard, filling different parts of the image. Use inner corner counts, not square counts.</p>
      </div>
      <form id="checkerboard-form" @submit.prevent="calculate">
        <div class="fields-grid">
          <label class="field"><span>INNER CORNER COLUMNS</span><input name="columns" type="number" min="3" value="9" required></label>
          <label class="field"><span>INNER CORNER ROWS</span><input name="rows" type="number" min="3" value="6" required></label>
          <label class="field"><span>SQUARE SIZE · M</span><input name="square_size_m" type="number" min="0.001" step="any" value="0.025" required></label>
          <label class="field"><span>CALIBRATION IMAGES</span><input name="images" type="file" accept="image/*" multiple required></label>
        </div>
        <div class="checkerboard-actions">
          <span id="checkerboard-result" class="helper">{{ checkerboardResult }}</span>
          <button class="button secondary" type="submit" :disabled="calibrating">{{ calibrating ? 'Calibrating…' : 'Calculate intrinsics' }}</button>
        </div>
      </form>
    </article>
  </section>

`;

export default {
  name: 'CameraCalibration',
  setup() {
    const draft = reactive({});
    const distortion = ref('');
    const saving = ref(false);
    const calibrating = ref(false);
    const checkerboardResult = ref('Intrinsics will populate above for review. Save them when ready.');
    const recordingDetails = computed(() => dashboard.selectedRecordingDetails);
    const showDatasetCalibration = computed(() => !dashboard.fromVideoUpload
      && recordingDetails.value?.recording?.dataset_id === 'comma2k19');
    const datasetExplanation = computed(() => {
      const details = recordingDetails.value;
      const camera = details?.calibration;
      const summary = camera ? 'Camera model: ' + camera.width + ' × ' + camera.height + ' px, fx '
        + camera.fx.toFixed(1) + ', fy ' + camera.fy.toFixed(1) + '. Principal point: ('
        + camera.cx.toFixed(1) + ', ' + camera.cy.toFixed(1) + ') px. Assumed height: '
        + camera.height_m.toFixed(2) + ' m. ' : '';
      const orientation = details?.recording?.camera_pose_available === false
        ? 'Synchronized camera orientation is unavailable for this segment.'
        : 'Published intrinsics and synchronized camera orientation apply automatically.';
      return summary + orientation + ' Change Height above road below to adjust the distance scale. '
        + 'The saved lens values and mounting angles continue to configure your own camera and RoadSens recordings. '
        + 'Distance and hazard GPS remain provisional.';
    });

    watch(() => dashboard.config, config => {
      if (!config?.calibration) return;
      for (const path of paths) {
        const [section, key] = path.split('.');
        draft[path] = key ? config[section][key] : config[section];
      }
      distortion.value = config.calibration.distortion.join(', ');
    }, { immediate: true });

    async function save() {
      if (saving.value || !dashboard.config) return;
      if (dashboard.state?.status === 'running') {
        notify('Stop detection before changing camera calibration or inference settings.', true);
        return;
      }
      saving.value = true;
      try {
        const coefficients = distortion.value.split(/[\s,]+/).filter(Boolean).map(Number);
        if (!coefficients.every(Number.isFinite) || ![4, 5, 8, 12, 14].includes(coefficients.length)) {
          throw new Error('Enter 4, 5, 8, 12, or 14 numeric OpenCV distortion coefficients.');
        }
        const saved = {
          ...dashboard.config,
          calibration: { ...dashboard.config.calibration, distortion: coefficients },
          detection: { ...dashboard.config.detection },
          pose: { ...dashboard.config.pose },
        };
        for (const path of paths) {
          const [section, key] = path.split('.');
          const value = typeof draft[path] === 'string' ? draft[path].trim() : draft[path];
          if (key) saved[section][key] = value;
          else saved[section] = value;
        }
        const result = await api('/api/config', { method: 'POST', body: saved });
        dashboard.config = result.config || result;
        dashboard.model = { path: dashboard.config.detection.model_path, classes: [], status: 'Load on start' };
        notify('Configuration saved. Start a new detection session to use these settings.');
      } catch (error) {
        notify(error.message, true);
      } finally {
        saving.value = false;
      }
    }

    async function calculate(event) {
      if (calibrating.value) return;
      if (event.currentTarget.elements.images.files.length < 3) {
        notify('Select at least three checkerboard images from different angles. More varied views improve lens calibration.', true);
        return;
      }
      const data = new FormData(event.currentTarget);
      calibrating.value = true;
      try {
        const result = await api('/api/calibration/checkerboard', { method: 'POST', body: data });
        for (const [key, value] of Object.entries(result.calibration || {})) {
          if (key === 'distortion') distortion.value = value.join(', ');
          else if ('calibration.' + key in draft) draft['calibration.' + key] = value;
        }
        draft['calibration.verified'] = false;
        checkerboardResult.value = 'Reprojection RMS: ' + Number(result.rms_px).toFixed(3)
          + ' px. Intrinsics populated above. Verify mounting geometry, then save configuration.';
        notify('Checkerboard intrinsics calculated. Review the values above and save configuration.');
      } catch (error) {
        notify(error.message, true);
      } finally {
        calibrating.value = false;
      }
    }

    return { dashboard, go, groups, draft, distortion, saving, calibrating, checkerboardResult,
      showDatasetCalibration, datasetExplanation, save, calculate };
  },
  template: CALIBRATION_TEMPLATE,
};

