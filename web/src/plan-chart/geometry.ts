// Geometry for the plan chart's stacked panels.
//
// Pure number-to-path functions — no DOM, no charting library, no dependency
// beyond the standard library, so every shape on the chart is a value a test
// can assert on.
//
// Recharts draws one plot with one x-axis well. This chart is five plots that
// must share an x-axis *exactly* — a quarter has to sit at the same pixel in
// the price strip as in the cost strip, or reading a column stops working —
// plus a device matrix, which is not a plot at all. Owning the geometry is
// cheaper than persuading a chart library to do that.
//
// Every series is drawn stepped, because a quarter is a band of time and not an
// instant: the value holds from the start of the quarter to the start of the
// next one. `x` is therefore indexed in quarters, and `x(i + 1)` is the right
// edge of quarter `i`.

export interface Scale {
  (value: number): number;
  readonly domain: readonly [number, number];
  readonly range: readonly [number, number];
}

export const linearScale = (
  domain: readonly [number, number],
  range: readonly [number, number],
): Scale => {
  const [from, to] = domain;
  // A zero-width domain would divide by zero; one unit wide draws a flat line,
  // which is the honest picture of a series that never changed.
  const span = to - from || 1;
  return Object.assign(
    (value: number) => range[0] + ((value - from) / span) * (range[1] - range[0]),
    { domain, range },
  );
};

/** A tenth of a pixel. Past that the extra digits only make the DOM bigger. */
const fmt = (value: number): string =>
  String(Number.isFinite(value) ? Math.round(value * 1_000) / 1_000 : 0);

const isDrawable = (value: number | null | undefined): value is number =>
  value !== null && value !== undefined && Number.isFinite(value);

/** Runs of consecutive drawable values, so a gap in the data draws as a gap. */
const runs = (
  values: readonly (number | null)[],
): Array<{ start: number; values: number[] }> => {
  const found: Array<{ start: number; values: number[] }> = [];
  let current: { start: number; values: number[] } | null = null;
  values.forEach((value, index) => {
    if (!isDrawable(value)) { current = null; return; }
    if (current === null) {
      current = { start: index, values: [] };
      found.push(current);
    }
    current.values.push(value);
  });
  return found;
};

export const stepLinePath = (
  values: readonly (number | null)[],
  x: Scale,
  y: Scale,
): string => runs(values).map(run => run.values.map((value, offset) => {
  const index = run.start + offset;
  const at = fmt(y(value));
  return `${offset === 0 ? 'M' : 'L'}${fmt(x(index))},${at}L${fmt(x(index + 1))},${at}`;
}).join('')).join('');

export const stepAreaPath = (
  values: readonly (number | null)[],
  x: Scale,
  y: Scale,
  baseline = 0,
): string => {
  const floor = fmt(y(baseline));
  return runs(values).map(run => {
    const top = run.values.map((value, offset) => {
      const index = run.start + offset;
      const at = fmt(y(value));
      return `${offset === 0 ? 'M' : 'L'}${fmt(x(index))},${at}L${fmt(x(index + 1))},${at}`;
    }).join('');
    const end = run.start + run.values.length;
    return `${top}L${fmt(x(end))},${floor}L${fmt(x(run.start))},${floor}Z`;
  }).join('');
};

/** A stacked band, as [lower, upper] per quarter. */
export type Band = ReadonlyArray<readonly [number, number]>;

export const stepBandPath = (band: Band, x: Scale, y: Scale): string => {
  return runs(band.map(([lo, hi]) => Number.isFinite(lo) && Number.isFinite(hi) ? hi : null))
    .map(run => {
      const top = run.values.map((upper, offset) => {
        const index = run.start + offset;
        const at = fmt(y(upper));
        return `${offset === 0 ? 'M' : 'L'}${fmt(x(index))},${at}L${fmt(x(index + 1))},${at}`;
      }).join('');
      let bottom = '';
      for (let index = run.start + run.values.length - 1; index >= run.start; index -= 1) {
        const at = fmt(y(band[index][0]));
        bottom += `L${fmt(x(index + 1))},${at}L${fmt(x(index))},${at}`;
      }
      return `${top}${bottom}Z`;
    }).join('');
};

/**
 * A plain polyline through quarter midpoints.
 *
 * State of charge is the one quantity here that is not a rate held across a
 * quarter but a level moving through it, so it is the one series that is not
 * drawn stepped.
 */
export const midpointLinePath = (
  values: readonly (number | null)[],
  x: Scale,
  y: Scale,
): string => runs(values).map(run => run.values.map((value, offset) =>
  `${offset === 0 ? 'M' : 'L'}${fmt(x(run.start + offset + 0.5))},${fmt(y(value))}`,
).join('')).join('');

/** Running [lower, upper] pairs, bottom series first. */
export const stackBands = (
  series: ReadonlyArray<readonly number[]>,
): Array<Array<[number, number]>> => {
  const length = series[0]?.length ?? 0;
  const running = new Array<number>(length).fill(0);
  return series.map(values => values.map((value, index) => {
    // Missing bands have no geometry; available bands still show their known
    // contribution. The partial stack is not a reconstructed household total.
    if (!Number.isFinite(value)) return [NaN, NaN] as [number, number];
    const lower = running[index];
    running[index] += value;
    return [lower, running[index]] as [number, number];
  }));
};

/**
 * Ticks a person recognises: multiples of 1, 2 or 5 times a power of ten,
 * always including zero when zero is inside the domain.
 *
 * The step is chosen by how far the ideal spacing sits from each candidate
 * rather than by rounding up to the next one. Rounding up turned a request for
 * four gridlines on a 0–4.6 kW panel into a step of 2, so the axis read 0, 2, 4
 * where 0, 1, 2, 3, 4 was available and better.
 */
export const niceTicks = (min: number, max: number, count = 4): number[] => {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return [0];
  const raw = (max - min) / Math.max(1, count);
  if (raw <= 0) return [0];
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const error = raw / magnitude;
  const step = magnitude * (error >= 7.07 ? 10 : error >= 3.16 ? 5 : error >= 1.41 ? 2 : 1);
  const ticks: number[] = [];
  for (let tick = Math.ceil(min / step) * step; tick <= max + 1e-9; tick += step) {
    // Repeated addition accumulates float error, and an axis labelled "-0" is
    // a bug the reader can see.
    ticks.push(Math.round(tick * 1e6) / 1e6 || 0);
  }
  return ticks.length > 0 ? ticks : [0];
};

/**
 * Collapse runs of identical values into [from, to) spans.
 *
 * The device matrix is one cell per meter per quarter — twenty meters over
 * thirty-six hours is nearly three thousand rectangles, and most of them are
 * the same shade of nothing next to an identical neighbour.
 */
export const spansOf = <T>(values: readonly T[]): Array<{ from: number; to: number; value: T }> => {
  const spans: Array<{ from: number; to: number; value: T }> = [];
  let from = 0;
  for (let index = 1; index <= values.length; index += 1) {
    if (index === values.length || values[index] !== values[from]) {
      spans.push({ from, to: index, value: values[from] });
      from = index;
    }
  }
  return spans;
};

export interface BandLabelPlacement {
  /** Index into the bands that were passed in. */
  band: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Where a stacked band can carry its own name, and where it cannot.
 *
 * Two rules, both learned from the first attempt at this panel. A band gets a
 * label only where it is thick enough to hold one — otherwise the text spills
 * over its neighbours and reads as belonging to the wrong series. And labels
 * are placed thickest-first, each one dropped if it would overlap a label
 * already placed, because six bands all labelled at their own widest point
 * put four of them on top of each other in the middle of the day.
 *
 * Everything dropped here is still named in the legend and the tooltip, so a
 * missing label costs nothing but the convenience.
 */
export const placeBandLabels = (
  bands: readonly Band[],
  names: readonly string[],
  x: Scale,
  y: Scale,
  { minThickness = 26, charWidth = 5.8, padding = 12, height = 16 } = {},
): BandLabelPlacement[] => {
  const candidates = bands.flatMap((band, index) => {
    let best = -1;
    let thickest = 0;
    band.forEach((pair, quarter) => {
      const thickness = Math.abs(y(pair[0]) - y(pair[1]));
      if (thickness > thickest) { thickest = thickness; best = quarter; }
    });
    if (best < 0 || thickest < minThickness) return [];
    const width = (names[index]?.length ?? 0) * charWidth + padding;
    const centre = Math.min(
      Math.max((x(best) + x(best + 1)) / 2, x(0) + width / 2),
      x(band.length) - width / 2,
    );
    return [{
      band: index,
      x: centre,
      y: (y(band[best][0]) + y(band[best][1])) / 2,
      width,
      height,
      thickness: thickest,
    }];
  });

  const placed: BandLabelPlacement[] = [];
  for (const candidate of [...candidates].sort((a, b) => b.thickness - a.thickness)) {
    const clashes = placed.some(other =>
      Math.abs(other.x - candidate.x) < (other.width + candidate.width) / 2
      && Math.abs(other.y - candidate.y) < (other.height + candidate.height) / 2);
    if (clashes) continue;
    const { thickness: _thickness, ...placement } = candidate;
    placed.push(placement);
  }
  return placed.sort((a, b) => a.band - b.band);
};
