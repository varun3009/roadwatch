import { computed, createApp, onMounted, onUnmounted } from './vendor/vue.esm-browser.prod.js';
import { dashboard, go, startDashboard, tabs } from './shared.js';
import LiveView from './live.js';
import SubmissionView from './submit.js';
import ReplayView from './replay-view.js';
import CalibrationView from './calibration.js';
import RecordsView from './records.js';
import HazardReportDialog from './hazard-report.js';

createApp({
  components: { LiveView, SubmissionView, ReplayView, CalibrationView, RecordsView, HazardReportDialog },
  setup() {
    const title = computed(() => tabs.find(([id]) => id === dashboard.tab)?.[1]);
    const modelPath = computed(() => dashboard.model.path || dashboard.config?.detection?.model_path || 'best.pt');
    const modelName = computed(() => modelPath.value.split(/[\\/]/).at(-1));
    const modelClasses = computed(() => Object.values(dashboard.model.classes || {}).join(' · '));
    let stopDashboard;

    function followHash() {
      const hash = location.hash.slice(1);
      if (hash !== 'main') go(hash);
    }

    onMounted(() => {
      followHash();
      window.addEventListener('hashchange', followHash);
      stopDashboard = startDashboard();
    });
    onUnmounted(() => {
      window.removeEventListener('hashchange', followHash);
      stopDashboard?.();
    });

    return { dashboard, go, title, modelPath, modelName, modelClasses };
  },
}).mount('#roadwatch');
