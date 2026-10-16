import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent,
  TooltipComponent, AxisPointerComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { cssVar, useThemeName } from "./lib.js";

echarts.use([BarChart, LineChart, ScatterChart, DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent,
  MarkLineComponent, MarkPointComponent, TooltipComponent, AxisPointerComponent, CanvasRenderer]);

/** Colors resolved from CSS tokens at render time, so light/dark both stay validated. */
export function colors() {
  const v = (k) => cssVar(k);
  return {
    text: v("--text"), text2: v("--text-2"), muted: v("--muted"), grid: v("--grid"), axis: v("--axis"),
    surface: v("--surface"), surface3: v("--surface-3"), accent: v("--accent"),
    good: v("--good"), bad: v("--bad"), warn: v("--warn"),
    ultra: v("--tier-ultra"), smart: v("--tier-smart"), goodTier: v("--tier-good"), other: v("--tier-other"),
    s: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--series-${i}`)),
  };
}

export function baseOption(c, extra = {}) {
  return {
    animation: false,
    textStyle: { fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif", color: c.text2, fontSize: 11.5 },
    grid: { left: 8, right: 14, top: 14, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis", confine: true, backgroundColor: c.surface, borderColor: c.axis, borderWidth: 1,
      textStyle: { color: c.text, fontSize: 12 }, extraCssText: "border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.25);",
      axisPointer: { type: "line", lineStyle: { color: c.muted, width: 1 } },
    },
    ...extra,
  };
}

const pad = (x) => String(x).padStart(2, "0");
export function timeLabel(v) {
  const d = new Date(v);
  if (d.getHours() === 0 && d.getMinutes() === 0) return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.`;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function timeAxis(c, extra = {}) {
  return {
    type: "time", splitNumber: 5, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false },
    axisLabel: { color: c.muted, hideOverlap: true, formatter: timeLabel },
    splitLine: { show: false }, ...extra,
  };
}
export function valueAxis(c, fmt, extra = {}) {
  return {
    type: "value", axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: c.muted, formatter: fmt }, splitLine: { lineStyle: { color: c.grid, width: 1 } }, ...extra,
  };
}
export function catAxis(c, data, extra = {}) {
  return {
    type: "category", data, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false },
    axisLabel: { color: c.muted, hideOverlap: true }, ...extra,
  };
}

/** ECharts host. `build(c)` returns the option for the current palette; re-run on theme or data change. */
export function EChart({ build, deps = [], height = 260, group, onClick, onZoom, ariaLabel }) {
  const el = useRef(null);
  const inst = useRef(null);
  const theme = useThemeName();
  const clickRef = useRef(onClick);
  clickRef.current = onClick;
  const zoomRef = useRef(onZoom);
  zoomRef.current = onZoom;
  useEffect(() => {
    const ch = echarts.init(el.current, null, { renderer: "canvas" });
    inst.current = ch;
    if (group) { ch.group = group; echarts.connect(group); }
    ch.on("click", (p) => clickRef.current && clickRef.current(p));
    ch.on("datazoom", () => zoomRef.current && zoomRef.current(ch.getOption().dataZoom));
    const ro = new ResizeObserver(() => ch.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); ch.dispose(); };
  }, [group]);
  useEffect(() => {
    if (!inst.current) return;
    const opt = build(colors());
    inst.current.setOption(opt, { notMerge: true, lazyUpdate: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, ...deps]);
  return <div ref={el} style={{ width: "100%", height }} role="img" aria-label={ariaLabel} />;
}

export const tierColor = (c, t) => ({ ultra: c.ultra, smart: c.smart, good: c.goodTier }[t] || c.other);

export function tipRow(color, label, value) {
  return `<div style="display:flex;gap:10px;justify-content:space-between;align-items:center"><span style="display:inline-flex;align-items:center;gap:6px"><i style="width:9px;height:9px;border-radius:3px;background:${color};display:inline-block"></i>${label}</span><b style="font-variant-numeric:tabular-nums">${value}</b></div>`;
}
