import C from '../lib/colors.js';
import { h, text, heading, panel, svg } from '../lib/elements.js';
import {
  axes, smoothDecreasingCurve, stepFunctionCurves,
  stackedBarsDecreasing, logLogLine, arrowRight,
} from '../lib/charts.js';

const CHART_W = 320;
const CHART_H = 200;
const CHART_PAD = 28;
const SMALL_CHART_W = 260;
const SMALL_CHART_H = 140;
const SMALL_PAD = 22;

function chartWithLabels(chartSvg, xLabel, yLabel, width, height) {
  return h('div', { flexDirection: 'row', alignItems: 'center', marginTop: 8 },
    yLabel
      ? h('div', {
          flexDirection: 'column', justifyContent: 'center', width: 16, height,
        },
          h('div', {
            fontSize: 10, color: C.textMuted, transform: 'rotate(-90deg)',
            whiteSpace: 'nowrap',
          }, yLabel),
        )
      : null,
    h('div', { flexDirection: 'column', alignItems: 'center' },
      chartSvg,
      xLabel
        ? h('div', { fontSize: 10, color: C.textMuted, marginTop: 2 }, xLabel)
        : null,
    ),
  );
}

function leftPanel(spec) {
  const lp = spec.left_panel;
  const chart = svg(CHART_W, CHART_H, [
    ...axes(CHART_W, CHART_H, CHART_PAD),
    smoothDecreasingCurve(CHART_W, CHART_H, CHART_PAD),
  ]);
  return panel([
    heading(lp.heading),
    text(lp.description, { fontSize: 12, color: C.textSecondary, marginTop: 4 }),
    chartWithLabels(chart, lp.chart?.x_label, lp.chart?.y_label, CHART_W, CHART_H),
    lp.annotation
      ? text(lp.annotation, { fontSize: 11, fontStyle: 'italic', color: C.textLight, marginTop: 8 })
      : null,
  ], { flex: 1, minWidth: 0 });
}

function connectionArrow(spec) {
  const conn = spec.connection;
  return h('div', {
    flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
    width: 90, paddingLeft: 4, paddingRight: 4,
  },
    h('div', {
      fontSize: 10, fontWeight: 700, color: C.accent, textAlign: 'center',
      letterSpacing: 0.5, marginBottom: 6,
    }, conn.label),
    svg(60, 24, arrowRight(60, 24)),
  );
}

function rightPanel(spec) {
  const rp = spec.right_panel;
  const chart = svg(CHART_W, CHART_H, [
    ...axes(CHART_W, CHART_H, CHART_PAD),
    ...stepFunctionCurves(CHART_W, CHART_H, CHART_PAD),
  ]);

  const labelRow = (rp.left_label || rp.right_label)
    ? h('div', {
        flexDirection: 'row', justifyContent: 'space-between', width: '100%',
        marginTop: 4, gap: 8,
      },
        rp.left_label
          ? h('div', {
              fontSize: 9, color: C.accent, backgroundColor: C.secondaryLight,
              padding: '2px 6px', borderRadius: 3,
            }, rp.left_label)
          : null,
        rp.right_label
          ? h('div', {
              fontSize: 9, color: C.accent, backgroundColor: C.secondaryLight,
              padding: '2px 6px', borderRadius: 3,
            }, rp.right_label)
          : null,
      )
    : null;

  const annotations = (rp.annotations || []).map(a =>
    text(a, { fontSize: 11, fontStyle: 'italic', color: C.textLight, marginTop: 2 })
  );

  const examples = (rp.examples || []).map(e =>
    h('div', {
      fontSize: 10, color: C.textSecondary, marginTop: 2,
      paddingLeft: 8, borderLeft: `2px solid ${C.secondary}`,
    }, e)
  );

  return panel([
    heading(rp.heading),
    text(rp.description, { fontSize: 12, color: C.textSecondary, marginTop: 4 }),
    chartWithLabels(chart, null, null, CHART_W, CHART_H),
    labelRow,
    ...annotations,
    examples.length > 0
      ? h('div', { flexDirection: 'column', marginTop: 6, gap: 3 }, ...examples)
      : null,
  ], { flex: 1, minWidth: 0 });
}

function supportPanels(spec) {
  const panels = spec.support_panels || [];

  const renderSupport = (sp) => {
    let chart = null;
    if (sp.visual === 'stacked_bars_decreasing') {
      chart = svg(SMALL_CHART_W, SMALL_CHART_H, [
        ...axes(SMALL_CHART_W, SMALL_CHART_H, SMALL_PAD),
        ...stackedBarsDecreasing(SMALL_CHART_W, SMALL_CHART_H, SMALL_PAD),
      ]);
    } else if (sp.visual === 'loglog_line') {
      chart = svg(SMALL_CHART_W, SMALL_CHART_H,
        logLogLine(SMALL_CHART_W, SMALL_CHART_H, SMALL_PAD),
      );
    }

    return panel([
      heading(sp.heading, { fontSize: 14 }),
      text(sp.description, { fontSize: 11, color: C.textSecondary, marginTop: 4 }),
      chart ? h('div', { marginTop: 6, justifyContent: 'center' }, chart) : null,
      sp.data_note
        ? text(sp.data_note, { fontSize: 10, color: C.textMuted, marginTop: 4, fontStyle: 'italic' })
        : null,
    ], { flex: 1, minWidth: 0 });
  };

  return h('div', {
    flexDirection: 'row', gap: 16, width: '100%', marginTop: 12,
  }, ...panels.map(renderSupport));
}

function conclusionBox(spec) {
  if (!spec.conclusion) return null;
  return h('div', {
    backgroundColor: C.conclusionBg,
    borderLeft: `4px solid ${C.accent}`,
    borderRadius: 6,
    padding: '12px 20px',
    marginTop: 12,
    width: '100%',
  },
    h('div', { fontSize: 13, color: C.accentDark, lineHeight: 1.5 }, spec.conclusion),
  );
}

export default function comparisonDecomposition(spec) {
  return h('div', {
    flexDirection: 'column',
    width: '100%',
    height: '100%',
    padding: 28,
    backgroundColor: C.bg,
    fontFamily: 'DejaVu Sans',
  },
    // Title
    h('div', { flexDirection: 'column', alignItems: 'center', width: '100%' },
      h('div', { fontSize: 26, fontWeight: 700, color: C.text, textAlign: 'center' }, spec.title),
      spec.subtitle
        ? h('div', { fontSize: 15, color: C.textMuted, textAlign: 'center', marginTop: 4 }, spec.subtitle)
        : null,
    ),
    // Context
    spec.context
      ? h('div', {
          fontSize: 12, fontStyle: 'italic', color: C.textLight, textAlign: 'center',
          marginTop: 8, marginBottom: 12, paddingLeft: 40, paddingRight: 40,
        }, spec.context)
      : null,
    // Main panels row
    h('div', {
      flexDirection: 'row', alignItems: 'stretch', width: '100%',
      flex: 1, gap: 0, marginTop: 8,
    },
      leftPanel(spec),
      connectionArrow(spec),
      rightPanel(spec),
    ),
    // Support panels
    supportPanels(spec),
    // Conclusion
    conclusionBox(spec),
  );
}
