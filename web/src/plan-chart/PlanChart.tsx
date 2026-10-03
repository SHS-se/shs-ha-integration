// One chart, five panels, one shared time axis.
//
// The app's counterpart of the website's PlanPanels: the same hand-drawn SVG,
// the same palette and the same tooltip, over the plan horizon only. Each
// quantity gets its own strip and its own axis, stacked over the same
// intervals, so reading a moment in time is reading a column: what it cost,
// where the power came from, what used it, what was left in store, the temperatures
// expected.
//
// geometry.ts, price-bands.ts and power-flows.ts are the website's modules,
// unchanged. Nothing in this directory imports from the rest of the app except
// the wire types, so the directory is the unit a shared package would carry.

import {useMemo, useRef, useState} from 'react';
import type {KeyboardEvent, PointerEvent, ReactNode} from 'react';
import {
  linearScale, midpointLinePath, niceTicks, placeBandLabels, stackBands,
  stepAreaPath, stepBandPath, stepLinePath, type BandLabelPlacement, type Scale,
} from './geometry';
import {PRICE_RAMP_STEPS, priceBands, priceGradientStops} from './price-bands';
import {powerFlowMagnitudes} from './power-flows';
import type {Plan, Slot, Temperature} from '../types';
import './plan-chart.css';

const VIEW_W = 1160;
const MARGIN_LEFT = 58;
const MARGIN_RIGHT = 92;
const PLOT_W = VIEW_W - MARGIN_LEFT - MARGIN_RIGHT;
const RIGHT = MARGIN_LEFT + PLOT_W;
const GAP = 34;
/** Eight is the ceiling on hues a reader can tell apart; the rest join base load. */
const MAX_SERIES = 8;
const HOUR_MS = 3_600_000;

const COLOURS = {
  solar: 'var(--plan-solar)', grid: 'var(--plan-grid)', battery: 'var(--plan-battery)',
  ev: 'var(--plan-ev)', base: 'var(--plan-base)',
} as const;
const temperatureColour = (index: number): string => `var(--plan-load-${(index + 2) % 9})`;
const loadColour = (slot: number): string => `var(--plan-load-${slot})`;
const FLOW_NAMES = ['Solar', 'Battery out', 'Grid in', 'Battery in', 'Grid out'];

const kw = (watts: number | null): number | null =>
  watts === null || !Number.isFinite(watts) ? null : watts / 1_000;
const percent = (fraction: number | null): number | null =>
  fraction === null || !Number.isFinite(fraction) ? null : fraction * 100;
const startOf = (slot: Slot): number => new Date(slot.start).getTime();
const endOf = (slot: Slot): number => startOf(slot) + slot.duration_hours * HOUR_MS;

/** A device the plan names only by its key still deserves a readable label. */
const displayName = (key: string, name: string | undefined): string => {
  const text = (name && name !== key ? name : key.replaceAll('_', ' ')).trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
};
const INLINE_NAME_MAX = 20;
const shorten = (name: string): string =>
  name.length <= INLINE_NAME_MAX ? name : `${name.slice(0, INLINE_NAME_MAX - 1)}…`;

/** Clock and calendar readings in the home's zone, never the browser's. */
const homeClock = (zone: string) => {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: zone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
  const read = (ms: number) => {
    const found: Record<string, string> = {};
    for (const part of parts.formatToParts(ms)) found[part.type] = part.value;
    return found;
  };
  return {
    hourMinute: (ms: number) => { const p = read(ms); return {hour: Number(p.hour), minute: Number(p.minute)}; },
    time: (ms: number) => { const p = read(ms); return `${p.hour}:${p.minute}`; },
    dayMonth: (ms: number) => { const p = read(ms); return `${p.month}/${p.day}`; },
    dayKey: (ms: number) => { const p = read(ms); return `${p.year}-${p.month}-${p.day}`; },
  };
};

/**
 * Interval edges on the x axis.
 *
 * The website indexes x in quarters because every interval there is one. A
 * plan's first interval is whatever is left of the current quarter, so here
 * the edges sit at their real times and a fractional index interpolates
 * between them — which keeps every path function in geometry.ts usable as is.
 */
const timeScale = (edges: readonly number[]): Scale => {
  const last = edges.length - 1;
  const position = linearScale([edges[0], edges[last]], [MARGIN_LEFT, RIGHT]);
  return Object.assign((index: number) => {
    const clamped = Math.max(0, Math.min(last, index));
    const lower = Math.min(last - 1, Math.floor(clamped));
    return position(edges[lower] + (edges[lower + 1] - edges[lower]) * (clamped - lower));
  }, {domain: [0, last] as const, range: [MARGIN_LEFT, RIGHT] as const});
};

interface Panel { top: number; height: number }
interface LoadSeries { key: string; name: string; slot: number; values: (number | null)[] }

function PanelHeading({title, unit, y}: {title: string; unit: string; y: number}) {
  return <>
    <text x={MARGIN_LEFT} y={y} className="pc-heading">{title}</text>
    <text x={MARGIN_LEFT + title.length * 6.6 + 10} y={y} className="pc-axis">{unit}</text>
  </>;
}

function Gridlines({ticks, y, format}: {ticks: number[]; y: Scale; format: (tick: number) => string}) {
  return <>{ticks.map(tick => <g key={tick}>
    <line x1={MARGIN_LEFT} x2={RIGHT} y1={y(tick)} y2={y(tick)} className="pc-grid"/>
    <text x={MARGIN_LEFT - 8} y={y(tick) + 3} textAnchor="end" className="pc-axis">{format(tick)}</text>
  </g>)}</>;
}

/** The one label a line always earns: its own value, at its own end. */
function EndLabel({y, colour, children}: {y: number; colour: string; children: string}) {
  return <g>
    <circle cx={RIGHT} cy={y} r={3} fill={colour} className="pc-end-dot"/>
    <text x={RIGHT + 7} y={y + 3.5} className="pc-axis">{children}</text>
  </g>;
}

/** A band's own name, written inside it where there is room. */
function InlineLabel({placement, children}: {placement: BandLabelPlacement; children: ReactNode}) {
  return <g>
    <rect x={placement.x - placement.width / 2} y={placement.y - placement.height / 2}
      width={placement.width} height={placement.height} rx={3} className="pc-inline-box"/>
    <text x={placement.x} y={placement.y + 4} textAnchor="middle" className="pc-inline-text">{children}</text>
  </g>;
}

/** A swatch carries the identity; the text stays in ink. */
function Reading({name, value, colour}: {name: string; value: string; colour?: string}) {
  return <div className="pc-reading">
    <span><i style={{backgroundColor: colour ?? 'transparent'}} aria-hidden="true"/><b>{name}</b></span>
    <span>{value}</span>
  </div>;
}

export function PlanChart({slots, plan, selectedIndex = -1, onSelect}: {
  slots: Slot[]; plan: Plan; selectedIndex?: number; onSelect?: (index: number) => void;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const [pointer, setPointer] = useState<{left: number; top: number} | null>(null);
  const [day, setDay] = useState<string | null>(null);
  const clock = useMemo(() => homeClock(plan.timezone), [plan.timezone]);
  const now = Date.now();

  // --- What the whole plan holds, before any day is picked ------------------
  const whole = useMemo(() => {
    const days: {key: string; label: string; from: number; to: number}[] = [];
    slots.forEach((slot, index) => {
      const key = clock.dayKey(startOf(slot));
      const current = days.at(-1);
      if (current?.key === key) current.to = index + 1;
      else days.push({key, label: clock.dayMonth(startOf(slot)), from: index, to: index + 1});
    });

    // Colours follow the plan's device list, not the ones running today, so a
    // meter keeps its colour from one plan to the next.
    const listed = plan.devices.map(device => device.key);
    const seen = [...new Set(slots.flatMap(slot => Object.keys(slot.device_loads_w ?? {})))];
    const order = [...listed.filter(key => seen.includes(key)), ...seen.filter(key => !listed.includes(key))];
    const running = order.filter(key => slots.some(slot => (slot.device_loads_w?.[key] ?? 0) > 0));
    const drawn = running.slice(0, MAX_SERIES);
    const folded = running.slice(MAX_SERIES);
    const series: LoadSeries[] = drawn.map(key => ({
      key, slot: order.indexOf(key) % 9,
      name: displayName(key, plan.devices.find(device => device.key === key)?.name),
      values: slots.map(slot => slot.device_loads_w?.[key] ?? null),
    }));
    const base = slots.map(slot => slot.base_w === null ? null
      : slot.base_w + folded.reduce((sum, key) => sum + (slot.device_loads_w?.[key] ?? 0), 0));

    return {days, series, base};
  }, [clock, plan.devices, slots]);

  const picked = whole.days.find(entry => entry.key === day);
  const from = picked?.from ?? 0;
  const to = picked?.to ?? slots.length;
  const n = to - from;

  const geometry = useMemo(() => {
    const rows = slots.slice(from, to);
    const cut = <T,>(values: readonly T[]): T[] => values.slice(from, to);
    const edges = rows.length ? [...rows.map(startOf), endOf(rows[rows.length - 1])] : [0, 1];
    const x = timeScale(edges);
    const homeSoc = rows.map(row => percent(row.battery_soc));
    const evSoc = rows.map(row => percent(row.ev_soc));
    const showHome = homeSoc.some(value => value !== null);
    const showEv = evSoc.some(value => value !== null);
    const showSoc = showHome || showEv;

    // Room above the first heading for the NOW flag, which in a plan-only window
    // sits at the left edge, exactly where the headings are.
    const price: Panel = {top: 46, height: 68};
    const flow: Panel = {top: price.top + price.height + GAP, height: 126};
    const load: Panel = {top: flow.top + flow.height + GAP, height: 150};
    const soc: Panel = {top: load.top + load.height + GAP, height: showSoc ? 62 : 0};
    const temperature: Panel = {top: soc.top + (showSoc ? soc.height + GAP : 0), height: 96};
    const axisY = temperature.top + temperature.height;

    // --- Price: published and estimated intervals as separate runs ----------
    const buy = rows.map(row => row.shadow_import_sek_per_kwh);
    const sell = rows.map(row => row.shadow_export_sek_per_kwh);
    const quotedBuy = rows.map(row => row.binding ? row.shadow_import_sek_per_kwh : null);
    const modelledBuy = rows.map(row => row.binding ? null : row.shadow_import_sek_per_kwh);
    const bands = priceBands(buy);
    const priceMax = Math.max(0.5, ...buy.filter((value): value is number => value !== null)) * 1.15;
    const priceY = linearScale([0, priceMax], [price.top + price.height, price.top]);

    // --- Flows: into the house above zero, out of it below ------------------
    const flows = powerFlowMagnitudes(rows.map(row => ({
      solarW: row.pv_w, gridImportW: row.grid_import_w, gridExportW: row.grid_export_w,
      batteryChargeW: row.battery_charge_w, batteryDischargeW: row.battery_discharge_w,
    })));
    const toKw = (values: number[]) => values.map(watts => watts / 1_000);
    const toNegativeKw = (values: number[]) => values.map(watts => -watts / 1_000);
    const supply = stackBands([toKw(flows.solarDirect), toKw(flows.batteryOut), toKw(flows.gridIn)]);
    const disposal = stackBands([toNegativeKw(flows.batteryIn), toNegativeKw(flows.gridOut)]);
    const flowMax = Math.max(0.5, ...supply[2].map(pair => pair[1])) * 1.12;
    // The outer edge of a band stacked downwards is its second value.
    const flowMin = Math.min(-0.5, ...disposal[1].map(pair => Math.min(...pair)).filter(Number.isFinite)) * 1.1;
    const flowY = linearScale([flowMin, flowMax], [flow.top + flow.height, flow.top]);
    const flowLabels = placeBandLabels([...supply, ...disposal], FLOW_NAMES, x, flowY);

    // --- Consumption: base at the floor, movable loads riding on top --------
    const series = whole.series.map(entry => ({...entry, values: cut(entry.values)}));
    const base = cut(whole.base);
    const inKw = (values: (number | null)[]) => values.map(watts => watts === null ? NaN : watts / 1_000);
    const loadBands = stackBands([inKw(base), ...series.map(entry => inKw(entry.values))]);
    const loadMax = Math.max(
      0.5,
      ...loadBands.flatMap(band => band.map(pair => pair[1])).filter(Number.isFinite),
      ...rows.map(row => (row.pv_w ?? 0) / 1_000),
    ) * 1.1;
    const loadY = linearScale([0, loadMax], [load.top + load.height, load.top]);
    const loadLabels = placeBandLabels(loadBands, ['', ...series.map(entry => shorten(entry.name))], x, loadY);

    const socY = linearScale([0, 100], [soc.top + soc.height, soc.top]);

    const temperatures = plan.temperatures.map((entry, index) => ({
      ...entry, colour: temperatureColour(index),
      values: rows.map(row => row.temperatures_c[entry.key] ?? null),
    }));
    const knownTemperatures = temperatures.flatMap(entry => entry.values).filter(
      (value): value is number => value !== null && Number.isFinite(value),
    );
    const temperatureBounds = [...knownTemperatures, ...temperatures.flatMap(entry =>
      entry.target_c === null ? [] : [entry.target_c])];
    const temperatureMin = temperatureBounds.length ? Math.floor((Math.min(...temperatureBounds) - 0.3) * 2) / 2 : 0;
    const temperatureMax = temperatureBounds.length ? Math.ceil((Math.max(...temperatureBounds) + 0.3) * 2) / 2 : 1;
    const temperatureY = linearScale([temperatureMin, temperatureMax], [temperature.top + temperature.height, temperature.top]);

    const hours = (edges[edges.length - 1] - edges[0]) / HOUR_MS;
    const every = hours > 48 ? 6 : hours > 24 ? 3 : hours > 10 ? 2 : 1;
    const hourTicks = rows.map((row, index) => ({index, ms: startOf(row)})).filter(({ms}) => {
      const {hour, minute} = clock.hourMinute(ms);
      return minute === 0 && hour % every === 0;
    });
    // The divider marks the home's midnight, not the browser's.
    const dayTicks = rows.map((row, index) => ({index, ms: startOf(row)})).filter(({index, ms}) => {
      const {hour, minute} = clock.hourMinute(ms);
      return index > 0 && hour === 0 && minute === 0;
    });

    return {
      rows, edges, x, axisY, height: axisY + 42, showSoc, showHome, showEv, homeSoc, evSoc,
      price, priceY, priceMax, buy, sell, bands, quotedBuy, modelledBuy,
      hasModelledPrice: modelledBuy.some(value => value !== null),
      flow, flowY, flowMin, flowMax, supply, disposal, flowLabels,
      load, loadY, loadMax, loadBands, loadLabels, series, base,
      soc, socY, temperature, temperatureY, temperatures, knownTemperatures, temperatureMin, temperatureMax, hourTicks, dayTicks,
    };
  }, [clock, from, plan.temperatures, slots, to, whole]);

  const {
    rows, edges, x, axisY, height, showSoc, showHome, showEv, homeSoc, evSoc,
    price, priceY, priceMax, buy, sell, bands, quotedBuy, modelledBuy, hasModelledPrice,
    flow, flowY, flowMin, flowMax, supply, disposal, flowLabels,
    load, loadY, loadMax, loadBands, loadLabels, series, base,
    soc, socY, temperature, temperatureY, temperatures, knownTemperatures, temperatureMin, temperatureMax, hourTicks, dayTicks,
  } = geometry;

  if (n === 0) return <p className="muted">This plan has no intervals to draw.</p>;

  const gradientId = 'plan-price-ramp';
  const priceStroke = bands ? `url(#${gradientId})` : 'var(--plan-price-4)';
  const top = price.top - 14;
  const span = axisY - top;

  // Where "now" falls in the drawn window: left of it the forecast has
  // already elapsed, right of it is still to come.
  const nowX = now <= edges[0] ? MARGIN_LEFT : now >= edges[n] ? null
    : linearScale([edges[0], edges[n]], [MARGIN_LEFT, RIGHT])(now);
  const flag = nowX === null ? 0 : nowX + 98 > RIGHT ? nowX - 94 : nowX + 4;

  /**
   * Which interval a pointer is over, straight from its position. Read at
   * click time as well as on move: a tap has no preceding pointermove, so the
   * interval under the finger would otherwise be unreachable.
   */
  const intervalAt = (clientX: number): number | null => {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box || box.width === 0) return null;
    const viewX = (clientX - box.left) * (VIEW_W / box.width);
    if (viewX < MARGIN_LEFT || viewX > RIGHT) return null;
    const ms = edges[0] + ((viewX - MARGIN_LEFT) / PLOT_W) * (edges[n] - edges[0]);
    const index = edges.findIndex((edge, at) => at < n && ms >= edge && ms < edges[at + 1]);
    return index < 0 ? n - 1 : index;
  };

  const handleMove = (event: PointerEvent<SVGSVGElement>) => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const index = intervalAt(event.clientX);
    if (index === null) { setHover(null); setPointer(null); return; }
    const box = wrap.getBoundingClientRect();
    setHover(index);
    setPointer({left: event.clientX - box.left, top: event.clientY - box.top});
  };

  const handleKey = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      if (hover === null) return;
      event.preventDefault();
      onSelect?.(from + hover);
      return;
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const start = hover ?? Math.max(0, Math.min(n - 1, selectedIndex - from));
    const next = Math.max(0, Math.min(n - 1, start + (event.key === 'ArrowLeft' ? -1 : 1)));
    const box = svgRef.current?.getBoundingClientRect();
    const wrap = wrapRef.current?.getBoundingClientRect();
    if (!box || !wrap) return;
    const ratio = box.width / VIEW_W;
    setHover(next);
    setPointer({left: box.left - wrap.left + x(next + 0.5) * ratio, top: box.top - wrap.top + flow.top * ratio});
  };

  const selected = selectedIndex - from;
  const hovered = hover === null || hover >= n ? null : rows[hover];
  const today = clock.dayKey(now);
  const fixed = (value: number) => value.toFixed(Math.abs(value) >= 10 ? 0 : 1);

  return <div ref={wrapRef} className="plan-chart">
    {whole.days.length > 1 && <div className="pc-days" role="group" aria-label="Days shown">
      {whole.days.map(entry => <button key={entry.key} type="button" aria-pressed={day === entry.key}
        onClick={() => { setDay(entry.key); setHover(null); setPointer(null); }}>
        {entry.key === today ? 'Today' : entry.label}
      </button>)}
      <button type="button" aria-pressed={!picked} onClick={() => { setDay(null); setHover(null); setPointer(null); }}>All</button>
    </div>}
    <div className="pc-scroll">
      <svg ref={svgRef} viewBox={`0 0 ${VIEW_W} ${height}`} preserveAspectRatio="xMidYMid meet"
        role="img" tabIndex={0} style={onSelect ? {cursor: 'pointer'} : undefined}
        aria-label="Price, power flows, consumption, storage and temperature over the plan horizon. Arrow keys step through intervals; exact values are in the interval inspector and the data table."
        onPointerMove={handleMove} onPointerDown={handleMove} onPointerLeave={() => { setHover(null); setPointer(null); }}
        onKeyDown={handleKey}
        onClick={event => { const index = intervalAt(event.clientX) ?? hover; if (index !== null) onSelect?.(from + index); }}>
        {bands && <defs>
          <linearGradient id={gradientId} gradientUnits="userSpaceOnUse" x1={MARGIN_LEFT} y1={0} x2={RIGHT} y2={0}>
            {priceGradientStops(
              // The ramp is laid out in time, so an interval of uneven length
              // still changes colour exactly at its own edges.
              buy, bands,
            ).map((stop, index, stops) => {
              const share = Number.parseFloat(stop.offset) / 100 * n;
              const offset = (x(share) - MARGIN_LEFT) / PLOT_W * 100;
              return <stop key={`${index}-${stops.length}`} offset={`${offset.toFixed(3)}%`} stopColor={`var(--plan-price-${stop.step})`}/>;
            })}
          </linearGradient>
        </defs>}

        {/* Everything right of now is still to come. Said once, across every
            panel, rather than as a faint tint inside one of them. */}
        {nowX !== null && <rect x={nowX} y={top} width={RIGHT - nowX} height={span} fill="var(--plan-wash)"/>}

        {/* ---------------------------------------------------- Price --- */}
        <PanelHeading title="Price" y={top} unit={hasModelledPrice
          ? `${plan.currency}/kWh · buy, fixed colour scale · dashed = estimated, not a market price`
          : `${plan.currency}/kWh · buy, fixed colour scale`}/>
        {bands && <g>
          {Array.from({length: PRICE_RAMP_STEPS}, (_, step) => <rect key={step}
            x={RIGHT - PRICE_RAMP_STEPS * 13 - 6 + step * 13} y={price.top - 23} width={12} height={9}
            fill={`var(--plan-price-${step + 1})`}/>)}
          <text x={RIGHT - PRICE_RAMP_STEPS * 13 - 12} y={price.top - 15} textAnchor="end" className="pc-axis">{bands.min.toFixed(2)}</text>
          <text x={RIGHT + 2} y={price.top - 15} className="pc-axis">{bands.max.toFixed(2)}+</text>
        </g>}
        <Gridlines ticks={niceTicks(0, priceMax, 3)} y={priceY} format={tick => tick.toFixed(1)}/>
        <path d={stepAreaPath(quotedBuy, x, priceY, 0)} fill={priceStroke} fillOpacity={0.2}/>
        <path d={stepAreaPath(modelledBuy, x, priceY, 0)} fill={priceStroke} fillOpacity={0.08}/>
        <path d={stepLinePath(sell, x, priceY)} fill="none" className="pc-sell"/>
        <path d={stepLinePath(quotedBuy, x, priceY)} fill="none" stroke={priceStroke} strokeWidth={3} strokeLinejoin="round"/>
        {/* Same colour and same position — only the continuity differs, so a
            forecast never masquerades as a price the market has published. */}
        <path d={stepLinePath(modelledBuy, x, priceY)} fill="none" stroke={priceStroke} strokeWidth={2}
          strokeLinejoin="round" strokeDasharray="5 4"/>

        {/* ---------------------------------------------------- Flows --- */}
        <PanelHeading title="Power flows" unit="kW · above zero = into the house, below = out of it" y={flow.top - 14}/>
        <Gridlines ticks={niceTicks(flowMin, flowMax, 4)} y={flowY} format={fixed}/>
        {[COLOURS.solar, COLOURS.battery, COLOURS.grid].map((colour, index) =>
          <path key={`supply-${index}`} d={stepBandPath(supply[index], x, flowY)} fill={colour} fillOpacity={0.9}/>)}
        {[COLOURS.battery, COLOURS.grid].map((colour, index) =>
          <path key={`disposal-${index}`} d={stepBandPath(disposal[index], x, flowY)} fill={colour} fillOpacity={0.42}/>)}
        {/* A two-pixel gap of card colour keeps the stack from fusing. */}
        {[supply[0], supply[1]].map((band, index) =>
          <path key={`flow-sep-${index}`} d={stepLinePath(band.map(pair => pair[1]), x, flowY)} fill="none" className="pc-separator"/>)}
        <line x1={MARGIN_LEFT} x2={RIGHT} y1={flowY(0)} y2={flowY(0)} className="pc-zero"/>
        {flowLabels.map(placement => <InlineLabel key={placement.band} placement={placement}>{FLOW_NAMES[placement.band]}</InlineLabel>)}

        {/* ---------------------------------------------- Consumption --- */}
        <PanelHeading title="Consumption" unit="kW · schedulable devices individually, the rest in base load · dotted = solar" y={load.top - 14}/>
        <Gridlines ticks={niceTicks(0, loadMax, 3)} y={loadY} format={fixed}/>
        {loadBands.map((band, index) => <path key={index === 0 ? 'base' : series[index - 1].key}
          d={stepBandPath(band, x, loadY)} fillOpacity={0.92}
          fill={index === 0 ? COLOURS.base : loadColour(series[index - 1].slot)}/>)}
        {loadBands.slice(0, -1).map((band, index) =>
          <path key={`load-sep-${index}`} d={stepLinePath(band.map(pair => pair[1]), x, loadY)} fill="none" className="pc-separator"/>)}
        {loadLabels.filter(placement => placement.band > 0).map(placement =>
          <InlineLabel key={series[placement.band - 1].key} placement={placement}>{shorten(series[placement.band - 1].name)}</InlineLabel>)}
        {/* Solar as an outline over the stack, so "did the sun cover it?" is
            one comparison between two edges rather than a hunt through fills. */}
        <path d={stepLinePath(rows.map(row => kw(row.pv_w)), x, loadY)} fill="none" stroke={COLOURS.solar}
          strokeWidth={2} strokeDasharray="1 3" strokeLinecap="round"/>

        {/* -------------------------------------------------- Storage --- */}
        {showSoc && <>
          <PanelHeading title="Storage" unit="%" y={soc.top - 14}/>
          <Gridlines ticks={[0, 50, 100]} y={socY} format={tick => String(tick)}/>
          {showHome && <>
            <path d={stepAreaPath(homeSoc, x, socY, 0)} fill={COLOURS.battery} fillOpacity={0.2}/>
            <path d={stepLinePath(homeSoc, x, socY)} fill="none" stroke={COLOURS.battery} strokeWidth={2}/>
          </>}
          {showEv && <path d={midpointLinePath(evSoc, x, socY)} fill="none" stroke={COLOURS.ev} strokeWidth={2}/>}
          {homeSoc[n - 1] !== null && <EndLabel y={socY(homeSoc[n - 1]!)} colour={COLOURS.battery}>{`home ${Math.round(homeSoc[n - 1]!)}%`}</EndLabel>}
          {evSoc[n - 1] !== null && <EndLabel y={socY(evSoc[n - 1]!)} colour={COLOURS.ev}>{`car ${Math.round(evSoc[n - 1]!)}%`}</EndLabel>}
        </>}

        {/* ---------------------------------------------- Temperature --- */}
        <g id="plan-temperature">
          <PanelHeading title="Temperature" unit="°C · planned" y={temperature.top - 14}/>
          {knownTemperatures.length > 0 ? <>
            <Gridlines ticks={niceTicks(temperatureMin, temperatureMax, 4)} y={temperatureY} format={tick => tick.toFixed(1)}/>
            {temperatures.map(entry => <g key={entry.key} data-temperature={entry.key}>
              {entry.target_c !== null && <>
                <line x1={MARGIN_LEFT} x2={RIGHT} y1={temperatureY(entry.target_c)} y2={temperatureY(entry.target_c)}
                  stroke={entry.colour} strokeWidth={1} strokeDasharray="2 3"/>
                <text x={MARGIN_LEFT + 4} y={temperatureY(entry.target_c) - 3} className="pc-axis">
                  {`${entry.name} · ${entry.target_c.toFixed(1)} °C target`}
                </text>
              </>}
              <path d={midpointLinePath(entry.values, x, temperatureY)} fill="none"
                stroke={entry.colour} strokeWidth={2} strokeLinejoin="round">
                <title>{`${entry.name} · planned temperature`}</title>
              </path>
              {entry.values[n - 1] !== null && <EndLabel y={temperatureY(entry.values[n - 1]!)} colour={entry.colour}>
                {`${entry.values[n - 1]!.toFixed(1)} °C`}
              </EndLabel>}
            </g>)}
          </> : <text x={MARGIN_LEFT} y={temperature.top + 30} className="pc-axis">No temperature forecast in this plan.</text>}
        </g>

        {/* ------------------------------------------------- Chrome ----- */}
        {nowX !== null && <g>
          <line x1={nowX} x2={nowX} y1={top - 28} y2={axisY} className="pc-now"/>
          <rect x={flag} y={top - 28} width={90} height={15} rx={3} className="pc-now-flag"/>
          <text x={flag + 5} y={top - 17} className="pc-now-text">NOW · PLAN →</text>
        </g>}
        {selected >= 0 && selected < n && <rect x={x(selected)} y={top}
          width={Math.max(2, x(selected + 1) - x(selected))} height={span} className="pc-selected"/>}
        {hover !== null && hover < n && <g pointerEvents="none">
          <rect x={x(hover)} y={top} width={Math.max(1.5, x(hover + 1) - x(hover))} height={span} className="pc-hover"/>
          <line x1={x(hover + 0.5)} x2={x(hover + 0.5)} y1={top} y2={axisY} className="pc-hover-line"/>
        </g>}

        {/* ------------------------------------------------- X axis ----- */}
        <line x1={MARGIN_LEFT} x2={RIGHT} y1={axisY} y2={axisY} className="pc-grid"/>
        {hourTicks.map(({index, ms}) =>
          <text key={index} x={x(index)} y={axisY + 14} textAnchor="middle" className="pc-axis">{clock.time(ms)}</text>)}
        {dayTicks.map(({index, ms}) => <g key={`day-${index}`}>
          <line x1={x(index)} x2={x(index)} y1={axisY - 4} y2={axisY + 4} className="pc-grid"/>
          <text x={x(index)} y={axisY + 27} textAnchor="middle" className="pc-day">{clock.dayMonth(ms)}</text>
        </g>)}
      </svg>
    </div>

    {temperatures.length > 0 && <div className="pc-temperature-legend" aria-label="Temperature series">
      {temperatures.map(entry => <span key={entry.key}><i style={{backgroundColor: entry.colour}} aria-hidden="true"/>{entry.name}</span>)}
    </div>}

    {hovered && pointer && <PlanTooltip
      row={hovered} label={`${clock.dayMonth(startOf(hovered))}, ${clock.time(startOf(hovered))}`}
      elapsed={endOf(hovered) <= now} currency={plan.currency}
      series={series.map(entry => ({...entry, value: entry.values[hover!] ?? 0}))}
      base={base[hover!]} homeSoc={homeSoc[hover!]} evSoc={evSoc[hover!]} temperatures={temperatures.map(entry => ({...entry, value: entry.values[hover!]}))}
      left={pointer.left} top={pointer.top} bounds={wrapRef.current?.getBoundingClientRect() ?? null}/>}
  </div>;
}

/**
 * Only what the plan has happening in this interval, and in the colours it
 * was drawn in. A reader matching a band to a number should not have to count
 * bands, or read past a column of zeroes to find the two devices running.
 */
function PlanTooltip({row, label, elapsed, currency, series, base, homeSoc, evSoc, temperatures, left, top, bounds}: {
  row: Slot; label: string; elapsed: boolean; currency: string;
  series: {key: string; name: string; slot: number; value: number}[];
  base: number | null; homeSoc: number | null; evSoc: number | null;
  temperatures: (Temperature & {colour: string; value: number | null})[];
  left: number; top: number; bounds: DOMRect | null;
}) {
  // Magnitudes, like the panel: "Battery in −3.59 kW" says the same thing
  // twice, once in the label and once in a minus sign.
  const magnitudes = powerFlowMagnitudes([{
    solarW: row.pv_w, gridImportW: row.grid_import_w, gridExportW: row.grid_export_w,
    batteryChargeW: row.battery_charge_w, batteryDischargeW: row.battery_discharge_w,
  }]);
  const flows: [string, number, string][] = [
    ['Solar to house', magnitudes.solarDirect[0] / 1_000, COLOURS.solar],
    ['Grid in', magnitudes.gridIn[0] / 1_000, COLOURS.grid],
    ['Grid out', magnitudes.gridOut[0] / 1_000, COLOURS.grid],
    ['Battery out', magnitudes.batteryOut[0] / 1_000, COLOURS.battery],
    ['Battery in', magnitudes.batteryIn[0] / 1_000, COLOURS.battery],
  ];
  const running = series.filter(entry => entry.value >= 10);
  const price = (value: number) => `${value.toFixed(2)} ${currency}/kWh`;

  const width = 244;
  // Flip to the other side of the pointer rather than run off the edge, and
  // keep the whole card inside the chart.
  const offset = bounds && left + width + 24 > bounds.width ? -width - 16 : 16;
  const estimated = 190 + running.length * 18;
  const clampedTop = bounds
    ? Math.min(Math.max(4, top - 40), Math.max(4, bounds.height - estimated))
    : Math.max(4, top - 40);

  return <div role="tooltip" className="pc-tooltip" style={{left: Math.max(4, left + offset), top: clampedTop, width}}>
    <div className="pc-tooltip-title"><span>{label}</span><em>{elapsed ? 'elapsed forecast' : 'plan'}</em></div>
    {flows.filter(([, value]) => value >= 0.02).map(([name, value, colour]) =>
      <Reading key={name} name={name} colour={colour} value={`${value.toFixed(2)} kW`}/>)}
    <div className="pc-tooltip-group">
      <Reading name="House demand" value={row.load_w === null ? 'Unavailable' : `${(Math.abs(row.load_w) / 1_000).toFixed(2)} kW`}/>
      {running.map(entry =>
        <Reading key={entry.key} name={entry.name} colour={loadColour(entry.slot)} value={`${(entry.value / 1_000).toFixed(2)} kW`}/>)}
      <Reading name="Base load" colour={COLOURS.base} value={base === null ? 'Unavailable' : `${(base / 1_000).toFixed(2)} kW`}/>
    </div>
    <div className="pc-tooltip-group">
      {homeSoc !== null && <Reading name="Home battery" colour={COLOURS.battery} value={`${Math.round(homeSoc)} %`}/>}
      {evSoc !== null && <Reading name="Car battery" colour={COLOURS.ev} value={`${Math.round(evSoc)} %`}/>}
      {row.shadow_import_sek_per_kwh !== null &&
        <Reading name={row.binding ? 'Buy' : 'Buy (estimated)'} value={price(row.shadow_import_sek_per_kwh)}/>}
      {row.shadow_export_sek_per_kwh !== null &&
        <Reading name={row.binding ? 'Sell' : 'Sell (estimated)'} value={price(row.shadow_export_sek_per_kwh)}/>}
      {temperatures.map(entry => <Reading key={entry.key} name={`${entry.name} temperature`} colour={entry.colour}
        value={entry.value === null ? 'Unavailable' : `${entry.value.toFixed(1)} °C`}/>)}
    </div>
  </div>;
}
