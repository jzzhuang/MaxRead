import C from './colors.js';
import { svgPath, svgLine, svgRect, svgCircle, svgEl } from './elements.js';

export function axes(w, h, pad) {
  return [
    svgLine(pad, pad - 5, pad, h - pad, { stroke: C.axis }),
    svgLine(pad, h - pad, w - pad + 5, h - pad, { stroke: C.axis }),
    // arrowheads
    svgPath(`M ${pad - 3} ${pad + 2} L ${pad} ${pad - 5} L ${pad + 3} ${pad + 2}`,
      { stroke: C.axis, strokeWidth: 1.2, fill: 'none' }),
    svgPath(`M ${w - pad - 2} ${h - pad - 3} L ${w - pad + 5} ${h - pad} L ${w - pad - 2} ${h - pad + 3}`,
      { stroke: C.axis, strokeWidth: 1.2, fill: 'none' }),
  ];
}

export function smoothDecreasingCurve(w, h, pad) {
  const n = 80;
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const x = pad + t * (w - 2 * pad);
    const y = pad + (1 - (0.9 * Math.pow(1 - t * 0.9, 0.35) + 0.08)) * (h - 2 * pad);
    pts.push(`${x.toFixed(1)} ${y.toFixed(1)}`);
  }
  return svgPath('M ' + pts.join(' L '), { strokeWidth: 2.5 });
}

export function stepFunctionCurves(w, h, pad, numCurves = 7) {
  const colors = ['#2563eb', '#3b82f6', '#60a5fa', '#93c5fd', '#a78bfa', '#c084fc', '#818cf8'];
  const usableH = h - 2 * pad;
  return Array.from({ length: numCurves }, (_, c) => {
    const threshold = 0.08 + (c / (numCurves - 1)) * 0.78;
    const yHigh = pad + 5 + c * (usableH / (numCurves + 1));
    const yLow = yHigh + usableH / (numCurves + 1) * 0.6;
    const pts = [];
    for (let i = 0; i <= 60; i++) {
      const t = i / 60;
      const x = pad + t * (w - 2 * pad);
      const sig = 1 / (1 + Math.exp(-25 * (t - threshold)));
      const y = yHigh + (yLow - yHigh) * (1 - sig);
      pts.push(`${x.toFixed(1)} ${y.toFixed(1)}`);
    }
    return svgPath('M ' + pts.join(' L '), {
      stroke: colors[c % colors.length],
      strokeWidth: 1.5,
      opacity: 0.85,
    });
  });
}

export function stackedBarsDecreasing(w, h, pad, numBars = 7) {
  const usableW = w - 2 * pad;
  const usableH = h - 2 * pad;
  const barW = usableW / numBars * 0.7;
  const gap = usableW / numBars * 0.3;
  return Array.from({ length: numBars }, (_, i) => {
    const x = pad + i * (barW + gap);
    const barH = usableH * 0.9 * Math.pow(0.78, i);
    const y = h - pad - barH;
    return svgRect(x, y, barW, barH, {
      fill: C.accent,
      opacity: 0.5 + 0.5 * (1 - i / numBars),
      rx: 2,
    });
  });
}

export function logLogLine(w, h, pad) {
  const elements = [];
  // axes
  elements.push(...axes(w, h, pad));
  // line
  elements.push(svgPath(
    `M ${pad + 8} ${pad + 12} L ${w - pad - 8} ${h - pad - 12}`,
    { stroke: C.accent, strokeWidth: 2 }
  ));
  // scatter dots
  for (let i = 0; i < 14; i++) {
    const t = (i + 0.5) / 14;
    const x = pad + t * (w - 2 * pad);
    const yBase = pad + t * (h - 2 * pad);
    const jitter = Math.sin(i * 5.7 + 1.3) * 6;
    elements.push(svgCircle(x, yBase + jitter, 2.5, { fill: C.accent, opacity: 0.55 }));
  }
  return elements;
}

export function arrowRight(w, h) {
  const midY = h / 2;
  const headSize = 8;
  return [
    svgLine(4, midY, w - headSize - 2, midY, { stroke: C.accent, strokeWidth: 2 }),
    svgPath(
      `M ${w - 2} ${midY} L ${w - headSize - 2} ${midY - headSize / 2} L ${w - headSize - 2} ${midY + headSize / 2} Z`,
      { fill: C.accent, stroke: 'none' }
    ),
  ];
}
