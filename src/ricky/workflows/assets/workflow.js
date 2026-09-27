'use strict';
const view = JSON.parse(document.getElementById('workflow-data').textContent);
const $ = id => document.getElementById(id),
  ns = 'http://www.w3.org/2000/svg';
const graph = $('graph'),
  panel = $('inspector'),
  expanded = new Set();
let selected = null,
  selectedEdge = null,
  query = '',
  camera = [0, 0, 1000, 600],
  bounds = [1000, 600];
const enabled = {
  dependency: true,
  data: true,
  condition: true
};

function element(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function svg(tag, attrs, text) {
  const e = document.createElementNS(ns, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  if (text !== undefined) e.textContent = text;
  return e;
}

function button(text, fn, cls) {
  const b = element('button', text, cls);
  b.addEventListener('click', fn);
  return b;
}

function block(title, value, copy = false) {
  panel.append(element('h3', title));
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  panel.append(element('pre', text));
  if (copy) panel.append(button('Copy text', async () => {
    try {
      await navigator.clipboard.writeText(text);
      $('status').textContent = 'Copied to clipboard';
    } catch {
      $('status').textContent = 'Clipboard unavailable here; select and copy the text.'
    }
  }, 'copy'));
}

function disclosure(title, value) {
  const d = element('details');
  d.append(element('summary', title), element('pre', JSON.stringify(value, null, 2)));
  panel.append(d);
}

function binding(edge) {
  const b = button(edge.reference || `${edge.source} → ${edge.target}`, () => {
    selectedEdge = edge;
    selected = null;
    draw();
    showEdge(edge);
  }, 'binding');
  b.append(element('span', `${edge.target}.${edge.destination||'needs'} · ${edge.kind}`));
  return b;
}

function welcome() {
  panel.replaceChildren(element('div', 'DESIGN NOTES', 'eyebrow'), element('h2', 'Explore the blueprint'), element('p', 'Select a step to inspect its instructions and typed inputs. Select a connection to trace a handoff.'));
  block('Invocation arguments', view.args);
  panel.append(element('p', view.context_note));
  panel.append(element('p', `Up to ${view.max_parallel_steps} independent steps may run concurrently. Approvals and effectful tool steps remain exclusive.`));
}

function showStep(id) {
  const step = view.steps.find(s => s.id === id);
  if (!step) return;
  panel.replaceChildren(element('span', step.kind, 'badge'), element('h2', step.id));
  panel.append(element('p', `Needs: ${step.needs.join(', ')||'(root)'}${step.parent?' · foreach '+step.parent:''}`));
  if (step.kind === 'foreach') panel.append(button(expanded.has(id) ? 'Collapse body' : 'Expand body', () => {
    expanded.has(id) ? expanded.delete(id) : expanded.add(id);
    draw();
    fit();
    showStep(id);
  }));
  if (step.instruction !== null) {
    panel.append(element('h3', 'Instructions'), element('div', step.instruction_source, 'source'), element('pre', step.instruction));
    panel.append(button('Copy instructions', async () => {
      try {
        await navigator.clipboard.writeText(step.instruction);
        $('status').textContent = 'Instructions copied';
      } catch {
        $('status').textContent = 'Select the instructions to copy them.'
      }
    }, 'copy'));
    block('Skill guidance' + (step.skill ? ' · ' + step.skill : ''), step.skill_body === null ? 'No attached skill.' : step.skill_body, true);
    block('Available tools', step.tools.length ? step.tools : 'None');
  }
  const incoming = view.edges.filter(e => e.target === id && e.kind !== 'dependency'),
    outgoing = view.edges.filter(e => e.source === id && e.kind !== 'dependency');
  if (incoming.length) {
    panel.append(element('h3', 'Inputs & conditions'));
    incoming.forEach(e => panel.append(binding(e)));
  }
  const inputs = Object.fromEntries(Object.entries(step.declaration).filter(([k]) => ['inputs', 'args', 'collection', 'item_key', 'proposal', 'prompt', 'message'].includes(k)));
  if (Object.keys(inputs).length) disclosure('Full input expressions / literals', inputs);
  if (outgoing.length) {
    panel.append(element('h3', 'Consumers'));
    outgoing.forEach(e => panel.append(binding(e)));
  }
  if (step.output_schema !== null) disclosure('Output schema', step.output_schema);
  if (step.risk) panel.append(element('p', `Tool risk: ${step.risk} · Effect: ${step.effect}`));
  const policy = Object.fromEntries(Object.entries(step.declaration).filter(([k]) => !['instruction', 'instruction_file', 'inputs', 'args'].includes(k)));
  disclosure('Step declaration & policy', policy);
  if (step.instruction !== null) panel.append(element('p', view.context_note));
}

function select(id) {
  selected = id;
  selectedEdge = null;
  draw();
  showStep(id);
}

function showEdge(edge) {
  panel.replaceChildren(element('span', edge.kind, 'badge'), element('h2', 'Connection detail'));
  if (edge.reference) {
    block('Source reference', edge.reference);
    block('Destination', `${edge.target}.${edge.destination}`);
  } else panel.append(element('p', `${edge.target} waits for ${edge.source}. This does not imply a data handoff.`));
  for (const id of new Set([edge.source, edge.target]))
    if (view.steps.some(s => s.id === id)) panel.append(button('Inspect ' + id, () => select(id), 'step-link'));
}

function visibleId(id) {
  const s = view.steps.find(s => s.id === id);
  return s && s.parent && !expanded.has(s.parent) ? s.parent : id;
}

function related() {
  if (!selected && !selectedEdge) return null;
  if (selectedEdge) {
    const matching = selectedEdge.reference ? view.edges.filter(e => e.reference === selectedEdge.reference && e.source === selectedEdge.source) : [selectedEdge];
    return new Set(matching.flatMap(e => [visibleId(e.source), visibleId(e.target)]));
  }
  const seeds = [selected],
    ids = new Set(seeds);
  for (const direction of ['source', 'target']) {
    const found = new Set(seeds);
    let change = true;
    while (change) {
      change = false;
      for (const e of view.edges) {
        if (e.kind === 'dependency') continue;
        const from = direction === 'source' ? e.target : e.source,
          to = direction === 'source' ? e.source : e.target;
        if (found.has(from) && !found.has(to)) {
          found.add(to);
          change = true;
        }
      }
    }
    for (const id of found) ids.add(id);
  }
  return new Set([...ids].map(visibleId));
}

function draw() {
  graph.replaceChildren();
  const positions = new Map(),
    top = view.steps.filter(s => !s.parent),
    levels = new Map();

  function level(s) {
    if (levels.has(s.id)) return levels.get(s.id);
    const n = s.needs.length ? Math.max(...s.needs.map(id => level(top.find(t => t.id === id)))) + 1 : 1;
    levels.set(s.id, n);
    return n;
  }
  top.forEach(level);
  const rows = new Map();
  positions.set('$trigger', {
    x: 30,
    y: 70,
    w: 230,
    h: 82
  });
  for (const s of top) {
    const l = levels.get(s.id),
      y = rows.get(l) || 70,
      children = view.steps.filter(c => c.parent === s.id),
      h = expanded.has(s.id) ? 110 + children.length * 105 : 82;
    positions.set(s.id, {
      x: 30 + l * 320,
      y,
      w: 260,
      h
    });
    rows.set(l, y + h + 70);
    if (expanded.has(s.id)) children.forEach((c, i) => positions.set(c.id, {
      x: 50 + l * 320,
      y: y + 90 + i * 105,
      w: 220,
      h: 78
    }));
  }
  bounds = [Math.max(...[...positions.values()].map(p => p.x + p.w)) + 40, Math.max(...[...positions.values()].map(p => p.y + p.h)) + 70];
  const defs = svg('defs', {});
  for (const kind of ['dependency', 'data', 'condition']) {
    const marker = svg('marker', {
      id: 'arrow-' + kind,
      viewBox: '0 0 10 10',
      refX: 9,
      refY: 5,
      markerWidth: 6,
      markerHeight: 6,
      orient: 'auto-start-reverse'
    });
    marker.append(svg('path', {
      d: 'M 0 0 L 10 5 L 0 10 z',
      fill: kind === 'data' ? 'var(--data)' : kind === 'condition' ? 'var(--condition)' : 'var(--muted)'
    }));
    defs.append(marker);
  }
  graph.append(defs);
  for (const s of top)
    if (expanded.has(s.id)) {
      const p = positions.get(s.id);
      graph.append(svg('rect', {
        x: p.x - 10,
        y: p.y - 10,
        width: p.w + 20,
        height: p.h + 20,
        rx: 4,
        class: 'loop-frame'
      }));
    }
  const active = related();
  for (const [index, e] of view.edges.entries()) {
    if (!enabled[e.kind]) continue;
    const a = visibleId(e.source),
      b = visibleId(e.target);
    if (a === b) continue;
    const p = positions.get(a),
      q = positions.get(b);
    if (!p || !q) continue;
    let d;
    if (Math.abs(p.x - q.x) < 40) {
      const x = p.x + p.w / 2;
      d = `M ${x} ${p.y+Math.min(p.h,82)} V ${q.y}`;
    } else {
      const offset = {dependency: -8, data: 0, condition: 8}[e.kind];
      const start = p.x + p.w,
        end = q.x,
        sy = p.y + 40 + offset,
        ty = q.y + 40 + offset,
        mid = (start + end) / 2 + (index % 3) * 5;
      d = `M ${start} ${sy} H ${mid} V ${ty} H ${end}`;
    }
    const g = svg('g', {
      class: active && (!active.has(a) || !active.has(b)) ? 'dim' : ''
    });
    g.append(svg('path', {
      d,
      class: `edge ${e.kind}${selectedEdge===e?' selected':''}`,
      'marker-end': `url(#arrow-${e.kind})`
    }));
    const hit = svg('path', {
      d,
      class: 'hit',
      tabindex: 0,
      role: 'button',
      'aria-label': e.reference ? `${e.reference} to ${e.target}.${e.destination}` : `${a} before ${b}`
    });
    hit.append(svg('title', {}, e.reference ? `${e.reference} → ${e.target}.${e.destination}` : `${a} → ${b}`));
    const pick = () => {
      selectedEdge = e;
      selected = null;
      draw();
      showEdge(e);
    };
    hit.addEventListener('click', pick);
    hit.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        pick();
      }
    });
    g.append(hit);
    graph.append(g);
  }
  for (const [id, p] of positions) {
    const s = view.steps.find(s => s.id === id),
      matches = !query || (s && JSON.stringify(s).toLowerCase().includes(query));
    const dim = (active && !active.has(visibleId(id))) || !matches;
    const g = svg('g', {
      class: `node${selected===id?' selected':''}${dim?' dim':''}`,
      tabindex: 0,
      role: 'button',
      'aria-label': s ? `${id}, ${s.kind}` : 'Invocation arguments',
      'data-step': id
    });
    g.append(svg('rect', {
      x: p.x,
      y: p.y,
      width: p.w,
      height: Math.min(p.h, 82),
      rx: s && s.kind === 'approval' ? 15 : 2
    }));
    g.append(svg('text', {
      x: p.x + 14,
      y: p.y + 20,
      class: 'kind'
    }, s ? `${String(view.steps.indexOf(s)+1).padStart(2,'0')} / ${s.kind.toUpperCase()}` : '00 / INVOCATION'));
    const label = svg('text', {
      x: p.x + 14,
      y: p.y + 43,
      class: 'label'
    }, !s ? 'trigger arguments' : id.length > 28 ? id.slice(0, 25) + '…' : id);
    label.append(svg('title', {}, id));
    g.append(label);
    const note = s ? (s.kind === 'foreach' ? (expanded.has(id) ? '− body expanded' : '+ expand item graph') : s.instruction !== null ? 'instructions + typed inputs' : s.declaration.when ? 'conditional step' : s.effect && s.effect !== 'none' ? s.effect + ' effect' : 'inspect declaration') : Object.keys(view.args).length + ' declared arguments';
    g.append(svg('text', {
      x: p.x + 14,
      y: p.y + 65,
      class: 'note'
    }, note));
    const pick = () => s ? select(id) : welcome();
    g.addEventListener('click', pick);
    g.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        pick();
      }
    });
    graph.append(g);
  }
  applyCamera();
}

function applyCamera() {
  graph.setAttribute('viewBox', camera.join(' '));
}

function fit() {
  const r = graph.getBoundingClientRect(),
    ratio = r.width / Math.max(r.height, 1),
    w = Math.max(bounds[0], bounds[1] * ratio),
    h = w / ratio;
  camera = [-(w - bounds[0]) / 2, -(h - bounds[1]) / 2, w, h];
  applyCamera();
}

function zoom(f) {
  const w = camera[2] * f,
    h = camera[3] * f;
  if (w < 150 || w > 40000) return;
  camera = [camera[0] + (camera[2] - w) / 2, camera[1] + (camera[3] - h) / 2, w, h];
  applyCamera();
}
let drag = null;
graph.addEventListener('pointerdown', e => {
  if (e.target !== graph) return;
  drag = [e.clientX, e.clientY, ...camera];
  graph.setPointerCapture(e.pointerId);
});
graph.addEventListener('pointermove', e => {
  if (!drag) return;
  const r = graph.getBoundingClientRect();
  camera = [drag[2] - (e.clientX - drag[0]) * camera[2] / r.width, drag[3] - (e.clientY - drag[1]) * camera[3] / r.height, camera[2], camera[3]];
  applyCamera();
});
graph.addEventListener('pointerup', () => {
  drag = null;
});
graph.addEventListener('pointercancel', () => {
  drag = null;
});
graph.addEventListener('wheel', e => {
  e.preventDefault();
  zoom(e.deltaY > 0 ? 1.12 : 1 / 1.12);
}, {
  passive: false
});
$('fit').onclick = fit;
$('zoom-in').onclick = () => zoom(.8);
$('zoom-out').onclick = () => zoom(1.25);
$('clear').onclick = () => {
  selected = null;
  selectedEdge = null;
  query = '';
  $('search').value = '';
  draw();
  welcome();
};
for (const kind of Object.keys(enabled)) $(kind).onchange = () => {
  enabled[kind] = $(kind).checked;
  draw();
};
$('search').addEventListener('input', () => {
  query = $('search').value.toLowerCase().trim();
  selected = null;
  selectedEdge = null;
  draw();
  if (!query) {
    welcome();
    return;
  }
  const matches = view.steps.filter(s => JSON.stringify(s).toLowerCase().includes(query));
  panel.replaceChildren(element('div', 'SEARCH', 'eyebrow'), element('h2', `${matches.length} matching steps`));
  for (const s of matches) panel.append(button(`${s.id} · ${s.kind}`, () => {
    if (s.parent) {
      expanded.add(s.parent);
      draw();
      fit();
    }
    select(s.id);
  }, 'step-link'));
});
let theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
try {
  theme = localStorage.getItem('ricky-workflow-theme') || theme;
} catch {}

function setTheme() {
  document.documentElement.dataset.theme = theme;
  $('theme').textContent = theme === 'dark' ? 'Switch to light' : 'Switch to dark';
  $('theme').setAttribute('aria-pressed', String(theme === 'dark'));
}
setTheme();
$('theme').onclick = () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  setTheme();
  try {
    localStorage.setItem('ricky-workflow-theme', theme);
  } catch {}
};
$('name').textContent = view.identity;
$('description').textContent = view.description;
$('revision').textContent = 'REV ' + view.fingerprint.slice(0, 10);
$('counts').textContent = `${view.steps.length} STEPS / ${view.edges.filter(e=>e.kind==='data').length} DATA BINDINGS`;
document.title = view.identity + ' · Workflow blueprint';
draw();
fit();
welcome();
window.addEventListener('resize', fit);
