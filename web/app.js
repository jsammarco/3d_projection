import { Viewer } from './viewer.js';

const $ = id => document.getElementById(id);
const form = $('settingsForm');
const token = document.querySelector('meta[name="studio-token"]').content;
let defaults, sourceFile = null, artwork, imageMode = 'original', mode = 'model';
let revision = 0, buildSequence = 0, timer, activeJob, currentJob, currentScene, dirty = true;
let simulationSequence = 0, simulationTimer, toastTimer;
let viewer;
try { viewer = new Viewer($('viewport')); }
catch (error) { console.error(error); $('webglError').hidden = false; }

async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {...options.headers, ...(options.method === 'POST' ? {'X-Studio-Token': token} : {})}});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function badge(text, state = '') {
  $('readyBadge').className = `status-badge ${state}`;
  $('readyBadge').replaceChildren(document.createElement('span'), document.createTextNode(text));
}
function showError(error) { $('errorMessage').textContent = error.message; $('errorMessage').hidden = false; badge('Needs attention', 'error'); }
function toast(text) { clearTimeout(toastTimer); $('toast').textContent = text; $('toast').hidden = false; toastTimer = setTimeout(() => $('toast').hidden = true, 4500); }

function fillSettings(values) {
  for (const field of form.elements) if (field.name && field.name in values) field.value = values[field.name] ?? (values.base_thickness + values.depth/2);
  $('thresholdValue').value = form.elements.threshold.value;
}
function settings() {
  const value = {...defaults};
  for (const field of form.elements) if (field.name) value[field.name] = Number(field.value);
  value.depth = 2 * (value.light_height - value.base_thickness);
  return value;
}
function drawArtwork() {
  if (!artwork) return;
  const canvas = $('sourceCanvas');
  const ratio = artwork.width / artwork.height;
  canvas.width = Math.round(Math.min(540, 326*ratio));
  canvas.height = Math.round(canvas.width/ratio);
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  ctx.fillStyle = 'white'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(artwork, 0, 0, canvas.width, canvas.height);
  if (imageMode === 'mask') {
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
    for (let i = 0; i < pixels.data.length; i += 4) {
      const gray = .299*pixels.data[i] + .587*pixels.data[i+1] + .114*pixels.data[i+2];
      const level = gray <= Number(form.elements.threshold.value) ? 0 : 255;
      pixels.data[i] = pixels.data[i+1] = pixels.data[i+2] = level;
    }
    ctx.putImageData(pixels, 0, 0);
  }
}
async function loadArtwork(file) {
  const image = new Image();
  const url = file ? URL.createObjectURL(file) : '/api/sample';
  try {
    image.src = url;
    await image.decode();
    if (image.width*image.height > 20000000) throw new Error('Choose an image with fewer than 20 million pixels.');
    artwork = image; sourceFile = file;
    const name = file?.name || 'fish.jpg';
    $('imageName').textContent = name; $('imageName').title = name;
    $('projectTitle').textContent = name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').replace(/^./, c => c.toUpperCase()) + ' projector';
    drawArtwork();
  } finally { if (file) URL.revokeObjectURL(url); }
}
async function chooseFile(file) {
  if (!file) return;
  try {
    if (file.size > 25*1024*1024) throw new Error('Choose an image file under 25 MB.');
    await loadArtwork(file); changed();
  } catch (error) { showError(error.message ? error : new Error('This image could not be read.')); }
}

function changed() {
  revision++; dirty = true;
  $('saveButton').disabled = true;
  $('partsCard').hidden = true;
  $('errorMessage').hidden = true;
  $('changeStatus').textContent = 'Unsaved changes';
  $('staleNotice').hidden = !currentScene;
  $('staleNotice').textContent = $('autoUpdate').checked ? 'Settings changed · updating preview' : 'Settings changed · select Update model';
  badge('Preview needs update', 'busy');
  clearTimeout(timer);
  if ($('autoUpdate').checked && form.checkValidity()) timer = setTimeout(build, 850);
}

async function build() {
  clearTimeout(timer);
  if (!form.reportValidity()) return;
  const sequence = ++buildSequence, snapshot = revision;
  simulationSequence++;
  const previous = activeJob;
  activeJob = null;
  if (previous) api(`/api/jobs/${previous}/cancel`, {method: 'POST'}).catch(console.warn);
  $('errorMessage').hidden = true;
  $('buildProgress').hidden = false; $('saveButton').disabled = true;
  $('progressLabel').textContent = 'Reading image and settings'; $('progressBar').style.width = '5%';
  $('changeStatus').textContent = 'Building preview'; badge('Building', 'busy');
  const data = new FormData(); data.append('settings', JSON.stringify(settings()));
  if (sourceFile) data.append('image', sourceFile);
  let submitted;
  try {
    submitted = await api('/api/jobs', {method: 'POST', body: data});
    if (sequence !== buildSequence) { await api(`/api/jobs/${submitted.id}/cancel`, {method: 'POST'}); return; }
    activeJob = submitted.id;
    let job = submitted;
    while (['queued', 'building'].includes(job.state)) {
      if (sequence !== buildSequence) return;
      $('progressLabel').textContent = job.message;
      $('progressBar').style.width = `${job.progress}%`;
      await delay(350);
      job = await api(`/api/jobs/${job.id}`);
    }
    if (sequence !== buildSequence) return;
    if (job.state === 'error') throw new Error(job.error);
    if (job.state === 'cancelled') { $('changeStatus').textContent = 'Cancelled'; badge('Build cancelled', 'busy'); return; }
    if (snapshot !== revision) return;
    $('progressLabel').textContent = 'Loading the interactive preview';
    if (viewer) await viewer.setScene(job.scene);
    if (sequence !== buildSequence || snapshot !== revision) return;
    currentScene = job.scene; currentJob = job.id; dirty = false;
    $('emptyState').hidden = true; $('staleNotice').hidden = true;
    $('saveButton').disabled = false;
    $('metricSize').textContent = job.scene.model_size.map(n => +n.toFixed(1)).join(' × ') + ' mm';
    const b = job.scene.projection_bounds;
    $('metricProjection').textContent = `${Math.round(b[2]-b[0])} × ${Math.round(b[3]-b[1])} mm`;
    $('metricValidation').textContent = job.scene.watertight ? 'All 7 meshes watertight' : 'Check mesh geometry';
    $('buildTime').textContent = `Built in ${job.elapsed.toFixed(1)}s · ${job.scene.triangles.toLocaleString()} triangles · ${job.scene.volume_cm3} cm³ of material`;
    $('changeStatus').textContent = 'Up to date'; badge('Ready to print');
    $('emitterSize').value = 0; $('emitterSizeValue').textContent = 'Point';
    $('simulationHint').textContent = 'Ray-traced visibility · ideal point source';
    setProjection(job.scene.projection_url);
    $('partsList').replaceChildren(...job.scene.parts.map(part => {
      const li = document.createElement('li'), dot = document.createElement('span'), link = document.createElement('a');
      dot.className = 'part-dot'; link.href = part.url; link.download = part.file; link.title = `Save ${part.file}`;
      link.setAttribute('aria-label', `Save ${part.label}`);
      link.innerHTML = '<svg><use href="#i-download"/></svg>';
      li.append(dot, document.createTextNode(part.label), link); return li;
    }));
    updateModeUI();
  } catch (error) {
    if (sequence === buildSequence) { showError(error); $('changeStatus').textContent = 'Could not update'; }
  } finally {
    if (sequence === buildSequence) { activeJob = null; $('buildProgress').hidden = true; }
  }
}

function setProjection(url) { $('projectionThumb').src = url; $('projectionLarge').src = url; }
function updateModeUI() {
  document.querySelectorAll('[data-mode]').forEach(button => {
    const active = button.dataset.mode === mode;
    button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active));
  });
  $('viewportShell').classList.toggle('light-mode', mode === 'light');
  $('modelControls').hidden = mode !== 'model';
  $('simulationControls').hidden = mode !== 'light';
  $('projectionCard').hidden = mode !== 'light' || !currentScene;
  $('partsCard').hidden = mode === 'light' || !currentScene || dirty;
  $('wallView').hidden = mode !== 'light';
  $('viewLabel').textContent = {model:'ASSEMBLED MODEL', light:'LIGHT ON THE RECEIVING WALL', parts:'SEPARATE PARTS · PRINT ORIENTATION'}[mode];
}

async function simulate() {
  const seq = ++simulationSequence, id = currentJob, scene = currentScene;
  if (!id) return;
  const size = Number($('emitterSize').value);
  $('emitterSizeValue').textContent = size ? `${size.toFixed(1)} mm` : 'Point';
  $('simulationHint').textContent = size ? 'Tracing a finite light source…' : 'Ray-traced visibility · ideal point source';
  try {
    const result = size ? await api(`/api/jobs/${id}/simulate`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({emitter_size:size})}) : {mask:scene.mask_url, projection:scene.projection_url};
    if (seq !== simulationSequence || id !== currentJob || activeJob) return;
    await viewer?.setMask(result.mask); setProjection(result.projection);
    $('simulationHint').textContent = size ? `Ray-traced visibility · ${size.toFixed(1)} mm disk source (preview only)` : 'Ray-traced visibility · ideal point source';
  } catch (error) { if (seq === simulationSequence) showError(error); }
}

form.addEventListener('submit', event => {event.preventDefault(); build();});
form.addEventListener('input', event => {
  if (!event.target.name) return;
  $('thresholdValue').value = form.elements.threshold.value;
  if (event.target.name === 'threshold') drawArtwork();
  changed();
});
$('buildButton').addEventListener('click', build);
$('autoUpdate').addEventListener('change', () => {clearTimeout(timer); if ($('autoUpdate').checked && dirty) build();});
$('resetSettings').addEventListener('click', () => {fillSettings(defaults); drawArtwork(); changed();});
$('chooseImage').addEventListener('click', () => $('imageFile').click());
$('imageFile').addEventListener('change', event => {chooseFile(event.target.files[0]); event.target.value = '';});
$('useSample').addEventListener('click', async () => {try {await loadArtwork(null); changed();} catch (error) {showError(error);}});
for (const name of ['dragenter', 'dragover']) $('dropZone').addEventListener(name, event => {event.preventDefault(); $('dropZone').classList.add('dragging');});
for (const name of ['dragleave', 'drop']) $('dropZone').addEventListener(name, event => {event.preventDefault(); $('dropZone').classList.remove('dragging');});
$('dropZone').addEventListener('drop', event => chooseFile(event.dataTransfer.files[0]));
document.querySelectorAll('[data-image-mode]').forEach(button => button.addEventListener('click', () => {
  imageMode = button.dataset.imageMode;
  document.querySelectorAll('[data-image-mode]').forEach(b => b.classList.toggle('active', b === button)); drawArtwork();
}));
document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {mode = button.dataset.mode; updateModeUI(); viewer?.setMode(mode);}));
$('explode').addEventListener('input', event => viewer?.setExplode(Number(event.target.value)/100));
$('showHood').addEventListener('change', event => viewer?.setHood(event.target.checked));
$('resetCamera').addEventListener('click', () => viewer?.resetCamera());
$('wallView').addEventListener('click', () => viewer?.wallView());
for (const name of ['lightOn', 'brightness', 'falloff']) $(name).addEventListener('input', () => {
  $('brightnessValue').textContent = `${$('brightness').value}%`;
  viewer?.setLighting({on:$('lightOn').checked, brightness:Number($('brightness').value)/100, falloff:$('falloff').checked});
});
$('emitterSize').addEventListener('input', () => {
  simulationSequence++; clearTimeout(simulationTimer);
  $('emitterSizeValue').textContent = Number($('emitterSize').value) ? `${Number($('emitterSize').value).toFixed(1)} mm` : 'Point';
  simulationTimer = setTimeout(simulate, 250);
});
$('cancelBuild').addEventListener('click', () => {
  buildSequence++; clearTimeout(timer);
  if (activeJob) api(`/api/jobs/${activeJob}/cancel`, {method:'POST'}).catch(showError);
  activeJob = null; $('buildProgress').hidden = true; $('changeStatus').textContent = 'Cancelled';
  badge('Build cancelled', 'busy'); $('staleNotice').textContent = 'Preview is out of date · select Update model';
});
$('saveButton').addEventListener('click', () => {
  if (!currentScene || dirty) return;
  const link = document.createElement('a'); link.href = currentScene.download_url; link.download = '';
  document.body.append(link); link.click(); link.remove(); toast('Download started: 7 STLs, assembly guide, and project settings.');
});
$('projectionCard').addEventListener('click', () => $('projectionDialog').showModal());
$('closeProjection').addEventListener('click', () => $('projectionDialog').close());
$('projectionDialog').addEventListener('click', event => {if (event.target === $('projectionDialog')) $('projectionDialog').close();});

try {
  const response = await api('/api/defaults'); defaults = response.settings;
  fillSettings(defaults); await loadArtwork(null); await build();
} catch (error) {showError(error); $('emptyState').querySelector('p').textContent = 'Could not load the project. Check the app terminal.';}
