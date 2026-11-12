import { reactive, ref, shallowRef, watch } from './vendor/vue.esm-browser.prod.js';
import { api, notify } from './shared.js';
import { coordinateText, hazardClass } from './submit.js';

const selectedHazardId = ref('');
const query = (id) => '?hazard_id=' + encodeURIComponent(id);

export const HazardReportActions = {
  props: { hazardId: { type: String, required: true } },
  setup(props) {
    function openReport() {
      selectedHazardId.value = props.hazardId;
    }
    const incidentUrl = (id) => '/api/incident-report' + query(id);
    return { openReport, incidentUrl };
  },
  template: `
    <div class="hazard-report-actions" @click.stop>
      <button type="button" class="button secondary" @click="openReport">Report hazard</button>
      <a class="button secondary" :href="incidentUrl(hazardId)" target="_blank" rel="noopener"
         title="Open a printable report in a new tab">Create incident report</a>
    </div>
  `,
};

export default {
  setup() {
    const dialog = ref(null);
    const hazard = shallowRef(null);
    const details = reactive({ title: '', address: '', severity: 'medium', notes: '' });
    const loading = ref(false);
    const saving = ref(false);
    const error = ref('');

    watch(selectedHazardId, async (id, _, onCleanup) => {
      if (!id) return;
      const request = new AbortController();
      onCleanup(() => request.abort());
      hazard.value = null;
      error.value = '';
      loading.value = true;
      dialog.value.showModal();
      try {
        const saved = await api('/api/hazard-report' + query(id), { signal: request.signal });
        if (request.signal.aborted) return;
        Object.assign(details, {
          title: saved.title || '', address: saved.address || '', notes: saved.notes || '',
          severity: ['low', 'medium', 'high'].includes(saved.severity) ? saved.severity : 'medium',
        });
        hazard.value = saved;
      } catch (err) {
        if (!request.signal.aborted) error.value = err.message;
      } finally {
        if (!request.signal.aborted) loading.value = false;
      }
    });

    function closeDialog() {
      dialog.value.close();
      selectedHazardId.value = '';
    }

    function cancel() {
      if (!saving.value) closeDialog();
    }

    async function save() {
      if (saving.value || !hazard.value) return;
      saving.value = true;
      error.value = '';
      try {
        await api('/api/hazard-report' + query(hazard.value.id), { method: 'PUT', body: details });
        closeDialog();
        notify('Hazard report saved locally.');
      } catch (err) {
        error.value = err.message;
      } finally {
        saving.value = false;
      }
    }

    return { dialog, hazard, details, loading, saving, error, cancel, save, hazardClass, coordinateText };
  },
  template: `
    <dialog ref="dialog" class="hazard-report-dialog" aria-labelledby="hazard-report-title" @cancel.prevent="cancel">
      <form @submit.prevent="save">
        <h2 id="hazard-report-title">{{ hazard?.reported_at ? 'Edit hazard report' : 'Report hazard' }}</h2>
        <p v-if="loading" role="status">Loading the saved hazard…</p>
        <template v-if="hazard">
          <p>{{ hazardClass(hazard) }} · {{ coordinateText(hazard) }}</p>
          <fieldset class="fields-grid" :disabled="saving">
            <label class="field span-2"><span>REPORT TITLE</span>
              <input v-model="details.title" maxlength="240" :placeholder="hazardClass(hazard)">
            </label>
            <label class="field"><span>STREET OR LANDMARK</span><input v-model="details.address" maxlength="500"></label>
            <label class="field"><span>PRIORITY</span>
              <select v-model="details.severity"><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select>
            </label>
            <label class="field span-2"><span>OBSERVATION NOTES</span><textarea v-model="details.notes" rows="4" maxlength="4000"></textarea></label>
          </fieldset>
          <p class="helper">These details are saved locally and included in the incident report.</p>
        </template>
        <p v-if="error" class="video-error" role="alert">{{ error }}</p>
        <div class="hazard-report-dialog-actions">
          <button type="button" class="button secondary" :disabled="saving" @click="cancel">{{ hazard ? 'Cancel' : 'Close' }}</button>
          <button v-if="hazard" type="submit" class="button primary" :disabled="saving">{{ saving ? 'Saving…' : 'Save hazard report' }}</button>
        </div>
      </form>
    </dialog>
  `,
};
