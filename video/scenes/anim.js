// Deterministic build animations: render(t) sets every element's state for time t.
// data-at = start (s, narration time), data-dur = length (s),
// data-fx = rise|fade|pop|wipe|growy|growx|color|out
const OFF = 0.45;  // narration starts this far into each segment
const ease = x => 1 - Math.pow(1 - x, 3);
const back = x => { const c = 1.7; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
function hex(h) { return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); }
function render(t) {
  document.querySelectorAll('[data-at]').forEach(el => {
    const at = +el.dataset.at + OFF, d = +(el.dataset.dur || 0.6);
    const fx = el.dataset.fx || 'rise';
    const p = Math.max(0, Math.min(1, (t - at) / d)), e = ease(p);
    const s = el.style;
    if (fx === 'rise') { s.opacity = e; s.transform = `translateY(${(1 - e) * 36}px)`; }
    else if (fx === 'fade') { s.opacity = e; }
    else if (fx === 'out') { s.opacity = 1 - e; }
    else if (fx === 'pop') { s.opacity = Math.min(1, p * 4); s.transform = `scale(${p ? back(p) : 0})`; }
    else if (fx === 'wipe') { s.clipPath = `inset(-2000% ${(1 - e) * 100}% -2000% -50px)`; }
    else if (fx === 'growy') { s.transformOrigin = 'top'; s.transform = `scaleY(${e})`; }
    else if (fx === 'growx') { s.transformOrigin = 'left'; s.transform = `scaleX(${e})`; }
    else if (fx === 'count') {
      const to = +el.dataset.to, dp = +(el.dataset.dp || 0);
      s.opacity = p > 0 ? 1 : 0;
      el.textContent = (el.dataset.pre || '') + (to * e).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp }) + (el.dataset.post || '');
    }
    else if (fx === 'color') {
      const a = hex(el.dataset.from), b = hex(el.dataset.to);
      s.color = `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * e)).join(',')})`;
    }
  });
}
window.render = render;
