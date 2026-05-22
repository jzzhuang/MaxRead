import C from './colors.js';

export function h(type, style, ...children) {
  const flat = children.flat(Infinity).filter(c => c != null && c !== false);
  return {
    type,
    props: {
      style: { display: 'flex', ...style },
      children: flat.length === 1 ? flat[0] : flat,
    },
  };
}

export function text(content, style = {}) {
  return h('div', { fontSize: 14, color: C.text, ...style }, content);
}

export function heading(content, style = {}) {
  return h('div', { fontSize: 17, fontWeight: 700, color: C.text, ...style }, content);
}

export function panel(children, style = {}) {
  return h('div', {
    flexDirection: 'column',
    border: `1px solid ${C.border}`,
    borderRadius: 8,
    padding: 16,
    backgroundColor: C.panelBg,
    ...style,
  }, ...children);
}

export function svgEl(type, props) {
  return { type, props };
}

export function svg(width, height, children) {
  return {
    type: 'svg',
    props: {
      xmlns: 'http://www.w3.org/2000/svg',
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      children: Array.isArray(children) ? children : [children],
    },
  };
}

export function svgPath(d, extra = {}) {
  return svgEl('path', { d, fill: 'none', stroke: C.accent, strokeWidth: 2, ...extra });
}

export function svgLine(x1, y1, x2, y2, extra = {}) {
  return svgEl('line', { x1, y1, x2, y2, stroke: C.axis, strokeWidth: 1, ...extra });
}

export function svgRect(x, y, w, ht, extra = {}) {
  return svgEl('rect', { x, y, width: w, height: ht, fill: C.accent, ...extra });
}

export function svgCircle(cx, cy, r, extra = {}) {
  return svgEl('circle', { cx, cy, r, fill: C.accent, ...extra });
}
