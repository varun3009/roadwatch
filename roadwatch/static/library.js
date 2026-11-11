/* Photo evidence library, GPS submissions, and YOLO detection replay. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const node = (tag, text, className = '') => {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const finite = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  const fmt = (v, digits = 1) => finite(v) ? Number(v).toFixed(digits) : 'Unknown';
  const coords = (item) => finite(item?.latitude) && finite(item?.longitude) && Math.abs(Number(item.latitude)) <= 90 && Math.abs(Number(item.longitude)) <= 180;
  const coordinateText = (item) => coords(item) ? `${fmt(item.latitude, 6)}, ${fmt(item.longitude, 6)}` : 'GPS unavailable';
  const label = (item) => item.title || item.class_name || item.hazard_type || 'Road hazard';
  const stamp = (value) => {
    if (!value) return 'Date unavailable';
    const text = String(value);
    const date = new Date(text.includes('T') || /Z$|[+-]\d\d:\d\d$/.test(text) ? text : text.replace(' ', 'T') + 'Z');
    return Number.isNaN(date.getTime()) ? text : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  };
  const clock = (v) => {
    const total = Math.max(0, Number(v) || 0);
    return `${Math.floor(total / 60).toString().padStart(2, '0')}:${Math.floor(total % 60).toString().padStart(2, '0')}`;
  };
  const path = (id) => `/api/library/${encodeURIComponent(id)}`;
  const state = { offset: 0, limit: 18, total: 0, nearby: null, hazards: [], selected: null, detail: null,
    map: null, layer: null, loadVersion: 0, detailVersion: 0, detailDirty: false, mapVersion: '',
    live: {}, activeTab: location.hash.slice(1) || 'live', job: null, jobTimer: null, previewUrl: null,
    sources: [], sourcesPromise: null, replayInfo: null, replayTime: 0, playing: false, replayTimer: null,
    replayRequest: null, replayUrl: null, replayVersion: 0, seekTimer: null, libraryLoaded: false,
    photoFilter: 'all', recovery: null, recoveryItems: new Map(), recoveryTimer: null, recoveryJob: null,
    evidenceSelection: null, analysisId: null, analysisSource: null, replaySession: null,
    replayHazards: [], frameTask: Promise.resolve(), frameSequence: 0, replayPending: false,
    replayFindingsSignature: null };

  async function api(url, options = {}) {
    const request = { ...options, headers: { ...options.headers } };
    if (request.body && !(request.body instanceof FormData)) {
      request.headers['Content-Type'] = 'application/json';
      request.body = JSON.stringify(request.body);
    }
    const response = await fetch(url, request);
    let result = null;
    try { result = await response.json(); } catch (_) { /* The HTTP status is still useful. */ }
    if (!response.ok) {
      const detail = result?.detail;
      throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((entry) => entry.msg || 'Check the submitted fields.').join('; ') : `Request failed (${response.status}).`);
    }
    return result;
  }
  function message(id, text, error = false) {
    $(id).textContent = text;
    $(id).classList.toggle('library-error', error);
  }
  function go(tab) {
    const button = document.querySelector(`.nav-item[data-tab="${tab}"]`);
    if (button) button.click();
  }
  async function action(button, task, pending) {
    const text = button.textContent;
    button.disabled = true;
    if (pending) button.textContent = pending;
    try { return await task(); }
    finally { button.disabled = false; button.textContent = text; }
  }
  function fieldsToObject(form) {
    return Object.fromEntries([...new FormData(form)].filter(([, value]) => typeof value === 'string'));
  }
  function badge(text, status) { return node('span', text, `badge ${status ? `status-${status}` : 'neutral'}`); }
  function safeMedia(url) { return typeof url === 'string' && url.startsWith('/api/') ? url : null; }
  function hazardClass(item) { return item.class_name || item.hazard_type || 'Other'; }

  function renderStats(counts = {}) {
    const stats = [['Saved hazards', counts.total ?? state.total, 'Across all sessions'], ['Open', counts.open ?? 0, 'Ready for review'], ['Reported', counts.reported ?? 0, 'Marked by you'], ['Evidence frames', counts.evidence_frames ?? 0, 'Maximum 10 per hazard']];
    $('library-stats').replaceChildren(...stats.map(([name, count, note]) => {
      const stat = node('div', null, 'library-stat');
      stat.append(node('span', name), node('strong', String(count)), node('small', note));
      return stat;
    }));
  }
  function renderClassOptions(classes = []) {
    if (!Array.isArray(classes) || !classes.length) return;
    const select = $('library-class');
    const selected = select.value;
    const names = [...new Set([...classes, ...[...select.options].map((option) => option.value).filter(Boolean)])].sort((a, b) => a.localeCompare(b));
    const all = node('option', 'All classes'); all.value = '';
    select.replaceChildren(all, ...names.map((name) => { const option = node('option', name); option.value = name; return option; }));
    select.value = selected;
  }
  function renderHazards() {
    const fragment = document.createDocumentFragment();
    state.hazards.forEach((item) => {
      const card = node('article', null, 'panel hazard-library-card');
      const media = node('div', null, 'hazard-card-media');
      const thumbnail = safeMedia(item.thumbnail_url);
      const recovery = state.recoveryItems.get(item.id);
      const showMissing = (failed = false) => {
        media.replaceChildren();
        const placeholder = node('div', null, 'hazard-card-placeholder');
        placeholder.append(node('span', '▧', 'empty-symbol'), node('strong', failed ? 'Photo could not be loaded' : 'Evidence photo missing'), node('span', recovery?.reason || 'Restore a frame from the original recording.'));
        const recover = node('button', recovery?.recoverable === false ? 'Source unavailable' : 'Recover photo', 'button secondary'); recover.type = 'button'; recover.disabled = recovery?.recoverable === false;
        recover.addEventListener('click', (event) => { event.stopPropagation(); recoverEvidence(item.id); });
        placeholder.append(recover); media.append(placeholder);
      };
      if (thumbnail) {
        const image = node('img'); image.src = thumbnail; image.alt = `${hazardClass(item)} evidence`; image.loading = 'lazy';
        image.addEventListener('error', () => showMissing(true), { once: true });
        media.append(image);
      } else showMissing();
      const manual = item.source_type === 'manual' || item.origin === 'manual' || item.manual === true || item.detection_method === 'manual';
      const overlay = node('div', null, 'hazard-card-overlay');
      overlay.append(badge(manual ? 'Human observation' : hazardClass(item)), node('span', `${Math.min(10, Number(item.evidence_count) || 0)} photos`, 'hazard-photo-count'));
      media.append(overlay);
      if (thumbnail) {
        media.classList.add('has-photo'); media.tabIndex = 0; media.setAttribute('role', 'button'); media.setAttribute('aria-label', `View ${hazardClass(item)} photo evidence`);
        media.addEventListener('click', () => openDetail(item.id));
        media.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDetail(item.id); } });
      }
      const body = node('div', null, 'hazard-card-body');
      const top = node('div', null, 'hazard-card-topline');
      const status = item.status || 'open';
      top.append(badge(status.charAt(0).toUpperCase() + status.slice(1), status), node('span', stamp(item.created_at)));
      body.append(top, node('h2', label(item)), node('p', item.address || hazardClass(item)), node('p', coordinateText(item), 'hazard-card-coords'));
      if (finite(item.nearby_distance_m)) body.append(node('p', Number(item.nearby_distance_m) < 1000 ? `${fmt(item.nearby_distance_m, 0)} m from your search point` : `${fmt(Number(item.nearby_distance_m) / 1000, 2)} km from your search point`, 'library-filter-chip'));
      const summary = node('div', null, 'hazard-card-summary');
      summary.append(node('span', `${Math.min(10, Number(item.evidence_count) || 0)} / 10 evidence frames`), node('span', finite(item.confidence) ? `${fmt(Number(item.confidence) * 100, 0)}% model confidence` : manual ? 'Human observation' : 'Confidence unknown'));
      const open = node('button', 'Open evidence & report ↗', 'button secondary'); open.type = 'button';
      open.addEventListener('click', () => openDetail(item.id));
      body.append(summary, open); card.append(media, body); fragment.append(card);
    });
    $('hazard-library-list').replaceChildren(fragment);
    $('library-empty').hidden = state.hazards.length > 0;
    $('library-empty-text').textContent = state.nearby ? 'No saved hazards with GPS coordinates were found in this radius. Try a wider area or another point.' : $('library-search').value || $('library-status').value || $('library-class').value ? 'Try a different search or filter to find saved hazards.' : 'Run detection or report a hazard to build your evidence library.';
    $('library-previous').disabled = state.offset === 0;
    $('library-next').disabled = state.offset + state.limit >= state.total;
    $('library-page-status').textContent = state.total ? `${state.offset + 1}–${Math.min(state.offset + state.hazards.length, state.total)} of ${state.total} hazards${state.nearby ? ' · nearest first' : ''}` : '0 saved hazards';
    renderMap();
  }
  async function loadLibrary(reset = false) {
    if (reset) state.offset = 0;
    const version = ++state.loadVersion;
    message('library-result-status', 'Loading saved hazards…');
    const query = new URLSearchParams({ limit: state.limit, offset: state.offset, evidence_first: 'true' });
    if (state.photoFilter !== 'all') query.set('has_evidence', state.photoFilter === 'photos' ? 'true' : 'false');
    const search = $('library-search').value.trim();
    if (search) query.set('search', search);
    if ($('library-class').value) query.set('class_name', $('library-class').value);
    if ($('library-status').value) query.set('status', $('library-status').value);
    if (state.nearby) Object.entries(state.nearby).forEach(([key, value]) => query.set(key, value));
    try {
      const result = await api(`/api/library?${query}`);
      if (version !== state.loadVersion) return;
      state.hazards = result.hazards || []; state.total = Number(result.total) || 0; state.libraryLoaded = true;
      if (state.total && state.offset >= state.total) { state.offset = Math.floor((state.total - 1) / state.limit) * state.limit; await loadLibrary(false); return; }
      renderStats(result.counts); renderClassOptions(result.classes); renderHazards();
      message('library-result-status', `${state.total} saved hazard${state.total === 1 ? '' : 's'}${state.photoFilter === 'photos' ? ' with photos' : state.photoFilter === 'missing' ? ' awaiting photos' : ' · records with photos shown first'}${state.nearby ? ` within ${Number(state.nearby.radius_m) / 1000} km · nearest first` : ''}.`);
    } catch (error) {
      if (version === state.loadVersion) message('library-result-status', `Unable to load the library: ${error.message} Use Refresh to retry.`, true);
    }
  }
  function selectPhotoFilter(value) {
    state.photoFilter = value;
    ['all', 'photos', 'missing'].forEach((name) => {
      const button = $(`library-view-${name}`); button.classList.toggle('active', name === value); button.setAttribute('aria-pressed', name === value ? 'true' : 'false');
    });
    loadLibrary(true);
  }
  function renderRecoveryStatus(result) {
    state.recovery = result;
    state.recoveryItems = new Map((result.items || []).map((item) => [item.hazard_id, item]));
    $('library-photos-count').textContent = String(result.with_evidence ?? 0);
    $('library-missing-count').textContent = String(result.missing ?? 0);
    const active = Boolean(result.active || state.recoveryJob && ['queued', 'processing'].includes(state.recoveryJob.status));
    $('evidence-recovery-banner').hidden = !result.missing && !active;
    $('recovery-heading').textContent = active ? 'Restoring the photo evidence…' : `${result.missing || 0} hazard record${Number(result.missing) === 1 ? '' : 's'} need photos`;
    $('recovery-description').textContent = `${result.recoverable || 0} can be restored from the saved video${result.unavailable ? ` · ${result.unavailable} source${Number(result.unavailable) === 1 ? '' : 's'} unavailable` : ''}. Recovered frames are labeled so you can distinguish them from original detection captures.`;
    $('recover-all-evidence').disabled = active || !result.recoverable;
    $('recover-all-evidence').textContent = active ? 'Recovering…' : result.recoverable > 20 ? 'Recover next 20 photos' : 'Recover missing photos';
    if (result.job && ['queued', 'processing'].includes(result.job.status)) {
      state.recoveryJob = result.job;
      renderRecoveryJob(result.job);
      if (!state.recoveryTimer) state.recoveryTimer = setTimeout(() => pollRecovery(result.job.id), 1000);
    }
  }
  async function loadRecoveryStatus() {
    try { const result = await api('/api/library/evidence-status'); renderRecoveryStatus(result); return result; }
    catch (_) { /* Library records still work if a recovery status check fails. */ return null; }
  }
  function renderRecoveryJob(job) {
    state.recoveryJob = job;
    const active = ['queued', 'processing'].includes(job.status);
    $('evidence-recovery-banner').hidden = false;
    $('recovery-progress-wrap').hidden = false;
    $('recovery-progress').value = Number(job.progress) || 0;
    $('recovery-heading').textContent = active ? 'Restoring the photo evidence…' : job.status === 'error' ? 'Photo recovery needs attention' : 'Photo evidence restored';
    $('recovery-progress-text').textContent = `${job.processed || 0} / ${job.total || 0} processed · ${job.recovered || 0} restored${job.failed ? ` · ${job.failed} unavailable` : ''}`;
    message('recovery-description', job.message || 'Retrieving real source frames and saving them to SQLite.', job.status === 'error');
    $('recover-all-evidence').disabled = active;
    $('recover-all-evidence').textContent = active ? 'Recovering…' : 'Check for missing photos';
  }
  async function pollRecovery(id) {
    clearTimeout(state.recoveryTimer); state.recoveryTimer = null;
    try {
      const result = await api(`/api/library/evidence-jobs/${encodeURIComponent(id)}`);
      const job = result.job || result;
      const previous = state.recoveryJob?.processed;
      renderRecoveryJob(job);
      if (previous !== job.processed && state.activeTab === 'library') loadLibrary(false);
      if (['queued', 'processing'].includes(job.status)) state.recoveryTimer = setTimeout(() => pollRecovery(id), 1200);
      else {
        sessionStorage.removeItem('roadwatch_evidence_job');
        await loadRecoveryStatus();
        await loadLibrary(false);
        if (state.selected) {
          const detail = await api(path(state.selected));
          if (state.detailDirty) { state.detail = { ...state.detail, evidence: detail.evidence }; renderEvidence(state.detail); }
          else renderDetail(detail);
        }
      }
    } catch (error) {
      message('recovery-description', `Could not read recovery progress: ${error.message} Retrying…`, true);
      state.recoveryTimer = setTimeout(() => pollRecovery(id), 5000);
    }
  }
  async function recoverEvidence(id = null) {
    const button = id && state.selected === id ? $('detail-recover-evidence') : $('recover-all-evidence');
    try {
      await action(button, async () => {
        const result = await api(id ? `${path(id)}/recover-evidence` : '/api/library/recover-evidence', { method: 'POST', body: id ? {} : { limit: 20 } });
        const job = result.job || result;
        renderRecoveryJob(job);
        if (job.id) { sessionStorage.setItem('roadwatch_evidence_job', job.id); state.recoveryTimer = setTimeout(() => pollRecovery(job.id), 700); }
      }, 'Recovering…');
    } catch (error) {
      $('evidence-recovery-banner').hidden = false; message('recovery-description', error.message, true);
      if (id === state.selected) message('detail-recovery-reason', error.message, true);
    }
  }
  function renderMap() {
    if (state.activeTab !== 'library' || !window.L) return;
    const located = state.hazards.filter(coords);
    $('library-map').hidden = !located.length && !state.nearby;
    if ($('library-map').hidden) return;
    if (!state.map) {
      state.map = L.map('library-map', { scrollWheelZoom: false });
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 20, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(state.map);
      state.layer = L.layerGroup().addTo(state.map);
    }
    const signature = JSON.stringify([state.nearby, located.map((item) => [item.id, item.latitude, item.longitude])]);
    if (signature !== state.mapVersion) {
      state.mapVersion = signature; state.layer.clearLayers();
      located.forEach((item) => {
        const popup = node('div'); const open = node('button', 'Review hazard ↗', 'button secondary'); open.type = 'button';
        open.addEventListener('click', () => openDetail(item.id));
        popup.append(node('strong', label(item)), node('p', coordinateText(item)), open);
        L.circleMarker([Number(item.latitude), Number(item.longitude)], { radius: 7, color: '#ffe1b0', fillColor: '#eab96b', fillOpacity: 0.9, weight: 2 }).bindPopup(popup).addTo(state.layer);
      });
      if (state.nearby) {
        const center = [Number(state.nearby.latitude), Number(state.nearby.longitude)];
        const circle = L.circle(center, { radius: Number(state.nearby.radius_m), color: '#55b9e5', fillOpacity: 0.06, weight: 1 }).addTo(state.layer);
        L.circleMarker(center, { radius: 6, color: '#e4f6ff', fillColor: '#5baee5', fillOpacity: 1, weight: 2 }).bindTooltip('Your search point').addTo(state.layer);
        state.map.fitBounds(circle.getBounds(), { padding: [12, 12], maxZoom: 17 });
      } else if (located.length) state.map.fitBounds(L.latLngBounds(located.map((item) => [Number(item.latitude), Number(item.longitude)])), { padding: [25, 25], maxZoom: 17 });
    }
    setTimeout(() => state.map?.invalidateSize(), 50);
  }
  function validPoint(latitude, longitude) {
    if (!finite(latitude) || !finite(longitude) || Math.abs(Number(latitude)) > 90 || Math.abs(Number(longitude)) > 180) throw new Error('Enter a latitude from -90 to 90 and a longitude from -180 to 180.');
    return { latitude: Number(latitude), longitude: Number(longitude) };
  }
  async function nearbySearch() {
    state.nearby = { ...validPoint($('nearby-latitude').value, $('nearby-longitude').value), radius_m: Number($('nearby-radius').value) };
    $('nearby-clear').hidden = false;
    message('nearby-status', `Searching around ${fmt(state.nearby.latitude, 6)}, ${fmt(state.nearby.longitude, 6)}. Distances are straight-line estimates; records may use approximate positions.`);
    await loadLibrary(true);
  }
  function locate() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) { reject(new Error('Location is unavailable in this browser. Enter coordinates instead.')); return; }
      navigator.geolocation.getCurrentPosition(resolve, () => reject(new Error('Could not access your location. Allow location access or enter coordinates manually.')), { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
    });
  }

  function fact(name, value, url) {
    const element = node('div', null, 'detail-fact');
    element.append(node('span', name), node('strong', value));
    if (url) { const link = node('a', 'Open map ↗'); link.href = url; link.target = '_blank'; link.rel = 'noopener'; element.append(link); }
    return element;
  }
  function evidenceDescription(entry, index) {
    const recovered = Boolean(entry.reconstructed || entry.recovered || String(entry.source || entry.capture_kind || '').includes('recover'));
    const manual = String(entry.source || '').includes('manual') || entry.capture_kind === 'manual';
    const method = recovered ? entry.overlay_origin === 'current_model_prediction' ? 'Recovered photo · current YOLO reconstruction' : entry.overlay_origin === 'recovered_original_bbox' ? 'Recovered photo · stored observation box' : 'Recovered source photo · original box unavailable' : manual ? 'Manually supplied photo · human observation' : 'YOLO detection evidence';
    const at = finite(entry.video_time_s) ? entry.video_time_s : entry.time_s;
    const caption = `${finite(at) ? clock(at) : `Photo ${index + 1}`} · Frame ${entry.frame_index ?? index + 1}${finite(entry.confidence) ? ` · ${fmt(Number(entry.confidence) * 100, 1)}% confidence` : ''}`;
    return { method, caption, recovered };
  }
  function chooseEvidence(entry, index, item, evidence) {
    state.evidenceSelection = { entry, index, item, evidence };
    const source = safeMedia(entry.url) || `/api/evidence/${encodeURIComponent(entry.id)}.jpg`;
    const description = evidenceDescription(entry, index);
    $('evidence-hero').hidden = false;
    $('evidence-hero-image').src = source;
    $('evidence-hero-image').alt = `${hazardClass(item)} · ${description.method}`;
    $('evidence-hero-kind').textContent = description.method;
    $('evidence-hero-kind').className = `badge ${description.recovered ? 'provisional' : 'matched'}`;
    $('evidence-hero-caption').textContent = `${index + 1} / ${evidence.length} · ${description.caption}`;
    [...$('evidence-gallery').children].forEach((button, at) => { button.classList.toggle('selected', at === index); button.setAttribute('aria-pressed', at === index ? 'true' : 'false'); });
  }
  function zoomEvidence() {
    const selection = state.evidenceSelection;
    if (!selection) return;
    const { entry, index, item, evidence } = selection;
    const description = evidenceDescription(entry, index);
    $('evidence-dialog-title').textContent = `${hazardClass(item)} · Photo ${index + 1} of ${evidence.length}`;
    $('evidence-dialog-image').src = $('evidence-hero-image').src;
    $('evidence-dialog-caption').textContent = `${description.method} · ${description.caption} · ${coordinateText(item)}${entry.note ? ` · ${entry.note}` : ''}`;
    $('evidence-dialog').showModal();
  }
  function renderEvidence(result) {
    const item = result.hazard;
    const evidence = (result.evidence || []).slice(0, 10);
    const recovery = state.recoveryItems.get(item.id);
    $('detail-evidence-count').textContent = `${evidence.length} / 10`;
    $('evidence-empty').hidden = evidence.length > 0;
    $('evidence-hero').hidden = evidence.length === 0;
    $('detail-recovery-reason').textContent = recovery?.reason || 'Restore real frames from its saved source video. If the source is unavailable, upload a new photo for your report.';
    $('detail-recover-evidence').disabled = recovery?.recoverable === false;
    $('detail-recover-evidence').textContent = recovery?.recoverable === false ? 'Source unavailable' : 'Recover photos';
    $('evidence-gallery').replaceChildren(...evidence.map((entry, index) => {
      const button = node('button', null, 'evidence-thumb'); button.type = 'button';
      const image = node('img'); image.src = safeMedia(entry.url) || `/api/evidence/${encodeURIComponent(entry.id)}.jpg`; image.alt = `Evidence photo ${index + 1} for ${hazardClass(item)}`; image.loading = 'lazy';
      const at = finite(entry.video_time_s) ? entry.video_time_s : entry.time_s;
      button.append(image, node('span', `${index + 1} · ${finite(at) ? clock(at) : 'Saved photo'}`));
      button.addEventListener('click', () => chooseEvidence(entry, index, item, evidence));
      return button;
    }));
    if (evidence.length) chooseEvidence(evidence[0], 0, item, evidence);
    else state.evidenceSelection = null;
  }
  function renderDetail(result) {
    const item = result.hazard;
    if (!item) throw new Error('This hazard is no longer available.');
    state.detail = result; state.selected = item.id; state.detailDirty = false;
    $('hazard-detail').hidden = false;
    $('detail-title').textContent = label(item);
    $('detail-identity').textContent = `${item.id} · ${stamp(item.created_at)}`;
    const mapLink = coords(item) ? `https://www.openstreetmap.org/?mlat=${encodeURIComponent(item.latitude)}&mlon=${encodeURIComponent(item.longitude)}#map=19/${encodeURIComponent(item.latitude)}/${encodeURIComponent(item.longitude)}` : null;
    const confidence = finite(item.confidence) ? `${fmt(Number(item.confidence) * 100, 1)}% · model prediction` : 'Human observation / not scored';
    $('detail-facts').replaceChildren(fact('HAZARD CLASS', hazardClass(item)), fact('SAVED LOCATION', coordinateText(item), mapLink), fact('CONFIDENCE', confidence), fact('OBSERVATIONS', `${item.observations ?? 1} observation${Number(item.observations) === 1 ? '' : 's'}`));
    const notes = [...new Set([item.location_note, item.accuracy_note].filter(Boolean))];
    if (!notes.length && item.location_method) notes.push(String(item.location_method).replace(/_/g, ' '));
    if (item.provisional && !notes.length) notes.push('Location and distance estimates are provisional. Camera calibration, road slope, and GPS quality affect their accuracy.');
    if (item.location_reference === 'capture') notes.unshift('Supplied coordinates identify the camera position, used as an approximate hazard-location proxy.');
    if (item.location_reference === 'hazard') notes.unshift('Hazard coordinates were supplied by the person making this report.');
    $('detail-location-note').textContent = notes.join(' ');
    renderEvidence(result);
    const form = $('complaint-form');
    ['title', 'municipal_authority', 'address', 'notes', 'reporter_name', 'reporter_contact'].forEach((name) => { form.elements[name].value = item[name] || ''; });
    form.elements.severity.value = ['low', 'medium', 'high'].includes(item.severity) ? item.severity : 'medium';
    form.elements.status.value = ['open', 'reported', 'resolved'].includes(item.status) ? item.status : 'open';
    $('complaint-print').href = `${path(item.id)}/complaint.html`;
    $('complaint-zip').href = `${path(item.id)}/complaint.zip`;
    message('complaint-save-status', 'Reports are drafts for your review. Downloading does not submit a complaint.');
    const session = result.session;
    $('detail-replay').hidden = session?.media_kind === 'photo' || (item.source_type === 'manual' && !session?.media_kind) || (!item.recording_id && !item.session_id && !session);
    $('detail-replay').onclick = () => openReplay(item.session_id ? `session:${item.session_id}` : `dataset:${item.recording_id}`, hazardVideoTime(item), item.recording_id ? `dataset:${item.recording_id}` : null);
  }
  async function openDetail(id) {
    if (state.detailDirty && !window.confirm('Leave your unsaved complaint edits?')) return;
    const version = ++state.detailVersion;
    message('library-result-status', 'Loading hazard evidence…');
    try {
      const result = await api(path(id));
      if (version !== state.detailVersion) return;
      renderDetail(result);
      $('hazard-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
      message('library-result-status', `${state.total} saved hazards. Reviewing ${hazardClass(result.hazard)}.`);
    } catch (error) { message('library-result-status', `Could not open the hazard: ${error.message}`, true); }
  }
  async function saveComplaint() {
    if (!state.selected) return;
    const saved = await api(path(state.selected), { method: 'PATCH', body: fieldsToObject($('complaint-form')) });
    const oldEvidence = state.detail?.evidence || []; const oldSession = state.detail?.session;
    renderDetail({ ...state.detail, hazard: saved.hazard || saved, evidence: oldEvidence, session: oldSession });
    message('complaint-save-status', 'Saved locally. Your complaint and evidence package are ready to download. Submit them through your municipal authority.');
    loadLibrary(false);
  }

  function updateSubmissionMode() {
    const manual = $('submission-form').elements.mode.value === 'manual';
    $('manual-class-field').hidden = !manual;
    $('submission-form').elements.hazard_type.required = manual;
    $('submission-file').required = !manual;
    $('submission-submit').textContent = manual ? 'Save manual hazard' : 'Detect and save hazards';
    if (!$('submission-file').files.length) $('submission-file-label').textContent = manual ? 'Add a photo or video · optional' : 'Choose a photo or video';
  }
  function updateLocationNote() {
    $('submission-location-note').textContent = $('submission-location-reference').value === 'hazard' ? 'You are identifying the hazard point yourself. For a video, this same point applies to every detection unless a synchronized GPS track is supplied.' : 'A camera GPS point is a location proxy, not a measured hazard center. For moving video, one fixed point applies to all detections unless a GPS track is supplied.';
  }
  function previewSubmission() {
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
    const file = $('submission-file').files[0];
    $('submission-preview').replaceChildren(); $('submission-preview').hidden = true;
    if (!file) { updateSubmissionMode(); return; }
    $('submission-file-label').textContent = file.name;
    if (file.type.startsWith('image/') || file.type.startsWith('video/')) {
      state.previewUrl = URL.createObjectURL(file);
      const media = node(file.type.startsWith('image/') ? 'img' : 'video');
      media.src = state.previewUrl;
      if (media.tagName === 'VIDEO') { media.controls = true; media.preload = 'metadata'; }
      else media.alt = 'Selected hazard photo preview';
      $('submission-preview').append(media); $('submission-preview').hidden = false;
    }
  }
  function renderJob(job) {
    state.job = job;
    $('submission-result').hidden = false;
    const progress = finite(job.progress) ? Math.min(100, Math.max(0, Number(job.progress))) : 0;
    $('submission-progress').value = progress;
    const status = job.status || 'queued';
    const labels = { uploading: 'Uploading your evidence…', queued: 'Your report is queued', processing: 'Detecting road hazards…', completed: 'Saved for your review', error: 'Your report needs attention' };
    $('submission-result-title').textContent = labels[status] || 'Processing your report…';
    const hazards = job.hazards || [];
    message('submission-result-message', job.error || job.message || (status === 'completed' ? hazards.length ? `${hazards.length} hazard${hazards.length === 1 ? '' : 's'} saved. Open the library to review the evidence and prepare your complaint.` : 'No supported hazards were detected. You can log what you observed using the manual option.' : `${progress.toFixed(0)}% processed. You can keep this page open while the work continues.`), status === 'error');
    $('submission-results-list').replaceChildren(...hazards.slice(0, 12).map((item) => node('div', `${hazardClass(item)} · ${coordinateText(item)} · ${Math.min(10, Number(item.evidence_count) || 0)} saved frames`, 'submission-result-item')));
    $('submission-open-library').hidden = !hazards.length;
    $('submission-submit').disabled = ['uploading', 'queued', 'processing'].includes(status);
    if (status === 'completed' || status === 'error') {
      clearTimeout(state.jobTimer); state.sources = [];
      sessionStorage.removeItem('roadwatch_submission_job');
      updateSubmissionMode();
      if (state.activeTab === 'library') loadLibrary(true);
    }
  }
  async function pollJob(id) {
    clearTimeout(state.jobTimer);
    try {
      const job = await api(`/api/submissions/${encodeURIComponent(id)}`);
      renderJob(job);
      if (!['completed', 'error'].includes(job.status)) state.jobTimer = setTimeout(() => pollJob(id), 1500);
    } catch (error) {
      message('submission-result-message', `Could not read progress: ${error.message} Retrying…`, true);
      state.jobTimer = setTimeout(() => pollJob(id), 5000);
    }
  }
  async function submitHazard() {
    const form = $('submission-form');
    const point = validPoint(form.elements.latitude.value, form.elements.longitude.value);
    const file = $('submission-file').files[0];
    if (file) {
      const isImage = file.type.startsWith('image/') || /\.(jpg|jpeg|png|webp)$/i.test(file.name);
      if (file.size > (isImage ? 25 : 250) * 1024 * 1024) throw new Error(isImage ? 'Choose a photo smaller than 25 MB.' : 'Choose a video smaller than 250 MB.');
    }
    const trackText = form.elements.gps_track.value.trim();
    if (trackText) {
      let track;
      try { track = JSON.parse(trackText); } catch (_) { throw new Error('The GPS track must be a valid JSON array.'); }
      if (!Array.isArray(track) || !track.length) throw new Error('The GPS track must be a nonempty JSON array of time_s, latitude and longitude values.');
    }
    const data = new FormData(form);
    data.set('latitude', point.latitude); data.set('longitude', point.longitude);
    if (!file) data.delete('file');
    if (!form.elements.gps_accuracy_m.value.trim()) data.delete('gps_accuracy_m');
    if (!trackText) data.delete('gps_track');
    if (form.elements.mode.value !== 'manual') data.delete('hazard_type');
    renderJob({ status: 'uploading', progress: 0, message: 'Uploading to your local RoadWatch server…', hazards: [] });
    $('submission-result').scrollIntoView({ behavior: 'smooth', block: 'center' });
    try {
      const job = await api('/api/submissions', { method: 'POST', body: data });
      renderJob(job);
      if (job.id && !['completed', 'error'].includes(job.status)) {
        sessionStorage.setItem('roadwatch_submission_job', job.id);
        state.jobTimer = setTimeout(() => pollJob(job.id), 800);
      }
    } catch (error) {
      renderJob({ status: 'error', progress: 0, error: error.message, hazards: [] });
      throw error;
    }
  }

  function detectorState(text, kind = 'idle') {
    $('replay-detector-status').replaceChildren(node('i'), document.createTextNode(text));
    $('replay-detector-status').dataset.state = kind;
  }
  function pauseReplay() {
    state.playing = false; clearTimeout(state.replayTimer);
    $('replay-play').textContent = state.replayInfo && state.replayTime >= state.replayInfo.duration_s ? 'Replay & detect' : 'Play & detect';
    if (state.analysisId && !state.replayPending) detectorState('Paused · detections saved', 'paused');
  }
  function replayClock() {
    $('replay-seek').value = state.replayTime;
    $('replay-clock').textContent = `${clock(state.replayTime)} / ${clock(state.replayInfo?.duration_s)}`;
  }
  function renderReplaySources() {
    const selected = $('replay-source').value;
    const query = ($('replay-source-search')?.value || '').trim().toLowerCase();
    const visible = state.sources.filter((source) => !query || `${source.label || ''} ${source.id}`.toLowerCase().includes(query) || source.id === selected);
    const initial = node('option', visible.length ? 'Choose a video to detect…' : 'No videos match this search'); initial.value = '';
    $('replay-source').replaceChildren(initial, ...visible.map((source) => { const option = node('option', source.label || source.id); option.value = source.id; return option; }));
    if (visible.some((source) => source.id === selected)) $('replay-source').value = selected;
    if ($('replay-source-search-status')) $('replay-source-search-status').textContent = `${visible.length} videos available`;
  }
  async function loadReplaySources(refresh = false) {
    if (state.sourcesPromise && !refresh) return state.sourcesPromise;
    const task = api('/api/replay/sources').then((result) => {
      state.sources = (result.sources || []).filter((source) => source.available !== false);
      renderReplaySources();
      return state.sources;
    }).catch((error) => {
      message('replay-status', `Could not load videos: ${error.message} Use Refresh videos to retry.`, true);
      throw error;
    }).finally(() => { state.sourcesPromise = null; });
    state.sourcesPromise = task;
    return task;
  }
  function hazardVideoTime(item) { return Number(item.first_video_time_s ?? item.video_time_s ?? item.first_seen_s) || 0; }
  function renderReplayFindings(hazards = [], evidenceCount) {
    state.replayHazards = hazards;
    $('replay-findings-count').textContent = String(hazards.length);
    $('replay-hazard-total').textContent = String(hazards.length);
    const photos = finite(evidenceCount) ? Number(evidenceCount) : hazards.reduce((sum, item) => sum + (Number(item.evidence_count) || 0), 0);
    $('replay-evidence-total').textContent = String(photos);
    $('replay-review-session').disabled = !hazards.length;
    const signature = JSON.stringify(hazards.map((item) => [item.id, hazardClass(item), item.confidence, item.latitude, item.longitude, item.evidence_count, item.thumbnail_url, hazardVideoTime(item)]));
    if (signature === state.replayFindingsSignature) return;
    state.replayFindingsSignature = signature;
    if (!hazards.length) {
      const empty = node('div', null, 'replay-log-empty');
      empty.append(node('span', '⌖'), node('strong', 'Your findings will appear here'), node('p', 'Play or seek through the video. Each tracked hazard keeps its own annotated evidence photos.'));
      $('replay-hazard-log').replaceChildren(empty);
      $('replay-hazard-ticks').replaceChildren();
      return;
    }
    const ordered = [...hazards].sort((left, right) => hazardVideoTime(left) - hazardVideoTime(right));
    $('replay-hazard-log').replaceChildren(...ordered.map((item) => {
      const row = node('article', null, 'replay-finding');
      const jump = node('button', null, 'replay-finding-jump'); jump.type = 'button';
      const image = node('img');
      if (safeMedia(item.thumbnail_url)) { image.src = item.thumbnail_url; image.alt = `${hazardClass(item)} saved evidence`; }
      else image.hidden = true;
      const content = node('div');
      const heading = node('div', null, 'replay-finding-title');
      heading.append(node('strong', hazardClass(item)), node('span', finite(item.confidence) ? `${fmt(Number(item.confidence) * 100, 0)}%` : ''));
      content.append(heading, node('span', `${clock(hazardVideoTime(item))} · #${item.track_id ?? String(item.id).split(':').pop()}`, 'replay-finding-time'), node('span', coordinateText(item), 'replay-finding-coordinates'), node('span', `${Math.min(10, Number(item.evidence_count) || 0)} / 10 photos saved`, 'replay-finding-photos'));
      jump.append(image, content);
      jump.addEventListener('click', () => seekReplay(hazardVideoTime(item)));
      const review = node('button', 'Open evidence ↗', 'text-button'); review.type = 'button';
      review.addEventListener('click', () => { go('library'); openDetail(item.id); });
      row.append(jump, review); return row;
    }));
    const duration = Number(state.replayInfo?.duration_s) || 1;
    $('replay-hazard-ticks').replaceChildren(...ordered.map((item) => {
      const tick = node('button'); tick.type = 'button';
      tick.style.left = `${Math.min(99, Math.max(0, hazardVideoTime(item) / duration * 100))}%`;
      tick.title = `${hazardClass(item)} at ${clock(hazardVideoTime(item))}`;
      tick.setAttribute('aria-label', `Jump to ${hazardClass(item)} at ${clock(hazardVideoTime(item))}`);
      tick.addEventListener('click', () => seekReplay(hazardVideoTime(item))); return tick;
    }));
  }
  function renderFrameDetections(detections = [], waiting = false) {
    const chips = detections.map((item) => {
      const chip = node('span', null, 'replay-detection-chip');
      chip.append(node('i'), node('strong', hazardClass(item)), node('span', `${fmt(Number(item.confidence) * 100, 0)}%${finite(item.distance_m) ? ` · ${fmt(item.distance_m)} m` : ''}${item.track_id !== null && item.track_id !== undefined ? ` · #${item.track_id}` : ''}`));
      return chip;
    });
    $('replay-detection-chips').replaceChildren(...(chips.length ? chips : [node('span', waiting ? 'Waiting for frame analysis…' : 'No supported anomalies detected in this frame', 'replay-clear-frame')]));
  }
  async function closeReplayAnalysis() {
    const identity = state.analysisId;
    state.analysisId = null; state.analysisSource = null;
    if (!identity) return;
    try {
      await state.frameTask.catch(() => {});
      await api('/api/replay/close', { method: 'POST', body: { analysis_id: identity } });
    } catch (_) { /* The server also releases an idle analysis lease automatically. */ }
  }
  async function ensureReplayAnalysis(version = state.replayVersion) {
    const source = $('replay-source').value;
    if (state.analysisId && state.analysisSource === source) return;
    detectorState('Preparing YOLO detection…', 'processing');
    const result = await api('/api/replay/analyze', { method: 'POST', body: { source_id: source } });
    if (version !== state.replayVersion || state.activeTab !== 'replay') {
      if (result.analysis_id) api('/api/replay/close', { method: 'POST', body: { analysis_id: result.analysis_id } }).catch(() => {});
      const error = new Error('Replay source changed'); error.name = 'AbortError'; throw error;
    }
    state.analysisId = result.analysis_id;
    state.analysisSource = source;
    state.replaySession = result.session_id;
    state.replayInfo = result.info;
    renderReplayFindings(result.hazards || [], result.evidence_count);
    return result;
  }
  async function loadReplayInfo(startAt = 0) {
    pauseReplay();
    const version = ++state.replayVersion;
    state.frameSequence += 1;
    state.replayRequest?.abort();
    const source = $('replay-source').value;
    const closing = closeReplayAnalysis();
    state.replayInfo = null; state.replaySession = null;
    $('replay-play').disabled = true; $('replay-seek').disabled = true; $('replay-restart').disabled = true;
    $('replay-frame').hidden = true; $('replay-empty').hidden = false;
    renderReplayFindings([]); renderFrameDetections([], true);
    if (!source) { detectorState('Ready to detect'); message('replay-status', 'Choose a video. Detection and evidence logging are enabled automatically.'); $('replay-source-meta').textContent = ''; return; }
    $('replay-processing').hidden = false;
    $('replay-processing-text').textContent = 'Loading YOLO and preparing your video…';
    message('replay-status', 'Preparing detection. The first frame can take a moment while your model loads.');
    try {
      await closing;
      if (version !== state.replayVersion) return;
      await ensureReplayAnalysis(version);
      const info = state.replayInfo;
      const duration = Number(info?.duration_s);
      if (!Number.isFinite(duration) || duration < 0) throw new Error('This video does not have a readable timeline.');
      state.replayTime = Math.max(0, Math.min(Number(startAt) || 0, Math.max(0, duration - 0.05)));
      $('replay-seek').max = Math.max(0.05, duration); $('replay-seek').disabled = false;
      $('replay-play').disabled = false; $('replay-restart').disabled = false;
      $('replay-source-meta').textContent = `${info.width} × ${info.height} · ${fmt(info.fps, 1)} source fps · ${clock(duration)} · YOLO enabled`;
      replayClock();
      await showReplayFrame(state.replayTime);
    } catch (error) {
      if (version === state.replayVersion && error.name !== 'AbortError') {
        detectorState('Detection unavailable', 'error');
        message('replay-status', `Could not start detection replay: ${error.message}`, true);
      }
    } finally { if (version === state.replayVersion) $('replay-processing').hidden = true; }
  }
  async function showReplayFrame(time_s) {
    const source = $('replay-source').value;
    const version = state.replayVersion;
    const sequence = ++state.frameSequence;
    if (!source) return false;
    const task = state.frameTask.catch(() => {}).then(async () => {
      if (version !== state.replayVersion || sequence !== state.frameSequence || state.activeTab !== 'replay') return false;
      state.replayPending = true;
      $('replay-processing').hidden = false;
      $('replay-processing-text').textContent = 'Detecting & saving evidence…';
      detectorState('YOLO detection & logging', 'processing');
      try {
        await ensureReplayAnalysis(version);
        const started = performance.now();
        const result = await api('/api/replay/process', { method: 'POST', body: { analysis_id: state.analysisId, time_s: Math.max(0, time_s) } });
        if (version !== state.replayVersion || sequence !== state.frameSequence || state.activeTab !== 'replay') return false;
        if (!/^data:image\/jpeg;base64,/.test(result.image_url || '') && !safeMedia(result.image_url)) throw new Error('The annotated detection frame was not returned.');
        $('replay-frame').src = result.image_url;
        $('replay-frame').hidden = false; $('replay-empty').hidden = true;
        if (finite(result.time_s)) state.replayTime = Number(result.time_s);
        $('replay-frame-meta').textContent = `Frame ${result.frame_index ?? '—'} · ${clock(result.time_s)}${result.cached ? ' · saved analysis' : ` · ${Math.round(performance.now() - started)} ms`}`;
        renderFrameDetections(result.detections || []);
        renderReplayFindings(result.hazards || [], result.evidence_count);
        replayClock();
        detectorState(result.cached ? 'Saved detection · no duplicate logs' : state.playing ? 'YOLO active · evidence logging' : 'Frame analyzed · evidence saved', result.cached ? 'cached' : 'active');
        message('replay-status', result.cached ? 'Reusing this annotated frame and its saved detections. Revisiting a frame does not duplicate hazard records.' : `${(result.detections || []).length} detection${(result.detections || []).length === 1 ? '' : 's'} in this frame · bounding boxes and evidence saved automatically. Playback adapts to inference speed.`);
        return true;
      } catch (error) {
        if (error.name !== 'AbortError' && version === state.replayVersion) {
          pauseReplay(); detectorState('Detection paused', 'error'); message('replay-status', error.message, true);
        }
        return false;
      } finally {
        state.replayPending = false;
        if (version === state.replayVersion) $('replay-processing').hidden = true;
      }
    });
    state.frameTask = task;
    return task;
  }
  async function seekReplay(time_s) {
    if (!state.replayInfo) return;
    pauseReplay();
    state.replayTime = Math.min(Math.max(0, Number(time_s) || 0), Math.max(0, Number(state.replayInfo.duration_s) - 0.05));
    replayClock(); await showReplayFrame(state.replayTime);
  }
  async function replayTick() {
    if (!state.playing || !state.replayInfo) return;
    const started = performance.now();
    const speed = Number($('replay-speed').value) || 1;
    state.replayTime = Math.min(state.replayInfo.duration_s, state.replayTime + 0.2 * speed);
    const requestedEnd = state.replayTime >= state.replayInfo.duration_s;
    const ok = await showReplayFrame(Math.min(state.replayTime, Math.max(0, state.replayInfo.duration_s - 0.05)));
    if (!ok) return;
    if (requestedEnd || state.replayTime >= state.replayInfo.duration_s - 0.06) {
      state.replayTime = state.replayInfo.duration_s;
      pauseReplay(); replayClock();
      detectorState('Replay complete · evidence saved', 'cached');
      message('replay-status', `Replay complete. ${state.replayHazards.length} hazards logged with their photo evidence. Open the library to review or create a complaint.`);
      closeReplayAnalysis(); return;
    }
    if (state.playing) state.replayTimer = setTimeout(replayTick, Math.max(0, 200 - (performance.now() - started)));
  }
  async function openReplay(source, startAt = 0, fallback = null) {
    go('replay');
    try {
      const sources = await loadReplaySources();
      const chosen = sources.find((entry) => entry.id === source) || sources.find((entry) => entry.id === fallback);
      if (!chosen) { message('replay-status', 'This session has no saved source video. Its captured evidence photos are available in the hazard library.', true); return; }
      if ($('replay-source-search')) $('replay-source-search').value = '';
      renderReplaySources(); $('replay-source').value = chosen.id;
      await loadReplayInfo(startAt);
    } catch (_) { /* A focused message is already visible above the video. */ }
  }
  function tabChanged(name) {
    const previous = state.activeTab;
    state.activeTab = name;
    if (name !== 'replay') {
      pauseReplay();
      if (previous === 'replay') { state.replayVersion += 1; state.frameSequence += 1; closeReplayAnalysis(); }
    }
    if (name === 'library') { loadLibrary(false); loadRecoveryStatus(); setTimeout(renderMap, 100); }
    if (name === 'replay' && !state.sources.length) loadReplaySources().catch(() => {});
  }

  $('library-filter-form').addEventListener('submit', (event) => { event.preventDefault(); loadLibrary(true); });
  $('library-refresh').addEventListener('click', () => { loadLibrary(false); loadRecoveryStatus(); });
  ['all', 'photos', 'missing'].forEach((name) => $(`library-view-${name}`).addEventListener('click', () => selectPhotoFilter(name)));
  $('recover-all-evidence').addEventListener('click', () => recoverEvidence());
  $('detail-recover-evidence').addEventListener('click', () => { if (state.selected) recoverEvidence(state.selected); });
  $('evidence-hero-zoom').addEventListener('click', zoomEvidence);
  $('evidence-hero-image').addEventListener('click', zoomEvidence);
  $('evidence-hero-image').addEventListener('error', () => { message('evidence-hero-caption', 'This saved photo could not be loaded. Try refreshing the library or recovering this record.', true); });
  document.querySelector('.library-nearby-drawer').addEventListener('toggle', () => { if (document.querySelector('.library-nearby-drawer').open) setTimeout(renderMap, 50); });
  $('library-status').addEventListener('change', () => loadLibrary(true));
  $('library-class').addEventListener('change', () => loadLibrary(true));
  $('library-previous').addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); loadLibrary(false); });
  $('library-next').addEventListener('click', () => { state.offset += state.limit; loadLibrary(false); });
  $('nearby-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await nearbySearch(); } catch (error) { message('nearby-status', error.message, true); } });
  $('nearby-clear').addEventListener('click', () => { state.nearby = null; $('nearby-clear').hidden = true; message('nearby-status', 'Showing saved hazards from all locations.'); loadLibrary(true); });
  $('nearby-location').addEventListener('click', async () => {
    try {
      await action($('nearby-location'), async () => { message('nearby-status', 'Waiting for location permission and a fresh position…'); const position = await locate(); $('nearby-latitude').value = position.coords.latitude; $('nearby-longitude').value = position.coords.longitude; await nearbySearch(); message('nearby-status', `Search point: ${fmt(position.coords.latitude, 6)}, ${fmt(position.coords.longitude, 6)} · browser accuracy ±${fmt(position.coords.accuracy, 0)} m. Distances are straight-line estimates.`); }, 'Locating…');
    } catch (error) { message('nearby-status', error.message, true); }
  });
  $('detail-close').addEventListener('click', () => { if (state.detailDirty && !window.confirm('Leave your unsaved complaint edits?')) return; state.detailVersion += 1; state.detailDirty = false; state.selected = null; $('hazard-detail').hidden = true; });
  $('complaint-form').addEventListener('input', () => { state.detailDirty = true; message('complaint-save-status', 'You have unsaved changes. Save complaint details before downloading the report.'); });
  $('complaint-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await action(event.submitter, saveComplaint, 'Saving…'); } catch (error) { message('complaint-save-status', error.message, true); } });
  ['complaint-print', 'complaint-zip'].forEach((id) => $(id).addEventListener('click', (event) => { if (state.detailDirty) { event.preventDefault(); message('complaint-save-status', 'Save complaint details first so your downloaded report includes the latest changes.', true); $('complaint-form').querySelector('button[type=submit]').focus(); } }));
  $('evidence-dialog-close').addEventListener('click', () => $('evidence-dialog').close());
  $('evidence-dialog').addEventListener('click', (event) => { if (event.target === $('evidence-dialog')) $('evidence-dialog').close(); });
  $('submission-form').elements.mode.forEach((input) => input.addEventListener('change', updateSubmissionMode));
  $('submission-file').addEventListener('change', previewSubmission);
  $('submission-location-reference').addEventListener('change', updateLocationNote);
  $('submission-location').addEventListener('click', async () => {
    try {
      await action($('submission-location'), async () => { const position = await locate(); $('submission-latitude').value = position.coords.latitude; $('submission-longitude').value = position.coords.longitude; $('submission-accuracy').value = Math.round(position.coords.accuracy * 10) / 10; $('submission-location-reference').value = 'capture'; updateLocationNote(); }, 'Locating…');
    } catch (error) { message('submission-location-note', error.message, true); }
  });
  $('submission-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await submitHazard(); } catch (error) { $('submission-result').hidden = false; message('submission-result-message', error.message, true); } });
  $('submission-open-library').addEventListener('click', () => { go('library'); const first = state.job?.hazards?.[0]; if (first) openDetail(first.id); });
  $('replay-refresh').addEventListener('click', () => action($('replay-refresh'), () => loadReplaySources(true), 'Refreshing…').catch(() => {}));
  $('replay-source').addEventListener('change', () => loadReplayInfo(0));
  if ($('replay-source-search')) $('replay-source-search').addEventListener('input', renderReplaySources);
  $('replay-play').addEventListener('click', () => {
    if (state.playing) { pauseReplay(); return; }
    if (!state.replayInfo) return;
    if (state.replayTime >= state.replayInfo.duration_s) state.replayTime = 0;
    state.playing = true; $('replay-play').textContent = 'Pause'; replayTick();
  });
  $('replay-restart').addEventListener('click', () => seekReplay(0));
  $('replay-seek').addEventListener('input', () => { pauseReplay(); state.frameSequence += 1; state.replayTime = Number($('replay-seek').value); replayClock(); clearTimeout(state.seekTimer); const target = state.replayTime; state.seekTimer = setTimeout(() => seekReplay(target), 150); });
  $('replay-review-session').addEventListener('click', () => { const first = state.replayHazards[0]; go('library'); if (first) openDetail(first.id); });
  $('replay-current').addEventListener('click', () => {
    const selected = $('recording-select').value;
    const session = state.live.session_id;
    openReplay(session ? `session:${session}` : `dataset:${selected}`, 0, selected ? `dataset:${selected}` : null);
  });
  window.addEventListener('roadwatch:tab', (event) => tabChanged(event.detail.name));
  window.addEventListener('roadwatch:state', (event) => { state.live = event.detail || {}; });
  window.addEventListener('beforeunload', () => {
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    if (state.replayUrl) URL.revokeObjectURL(state.replayUrl);
    state.replayRequest?.abort();
    if (state.analysisId) fetch('/api/replay/close', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ analysis_id: state.analysisId }), keepalive: true }).catch(() => {});
  });
  updateSubmissionMode();
  renderStats();
  tabChanged(state.activeTab);
  const pendingJob = sessionStorage.getItem('roadwatch_submission_job');
  if (pendingJob) pollJob(pendingJob);
  const recoveryJob = sessionStorage.getItem('roadwatch_evidence_job');
  if (recoveryJob) pollRecovery(recoveryJob);
})();

