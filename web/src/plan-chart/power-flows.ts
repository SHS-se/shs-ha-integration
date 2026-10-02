// The five flows the power panel stacks, as magnitudes.
//
// Import and export live in separate fields, as do charge and discharge, so
// which way the energy is going is already carried by *which field* a number is
// in. The sign is therefore redundant — and it disagrees with itself: the
// timeline stores export and charge negative (a hangover from a chart that drew
// them as dips on a single axis), while the plan contract and the ingest
// payload store them positive.
//
// Taking the magnitude of each one makes the panel independent of that. A
// band's side of the zero line is decided here, once, rather than inherited
// from whichever source happened to fill the row. Getting this wrong is not
// subtle but it is quiet: negating an already-negative charge drew the disposal
// bands *above* the axis, where semi-transparent green over orange reads as an
// unexplained brown, and inflated the solar band by the same amount.

export interface PowerFlowQuarter {
  solarW: number | null;
  gridImportW: number | null;
  gridExportW: number | null;
  batteryChargeW: number | null;
  batteryDischargeW: number | null;
}

export interface PowerFlowMagnitudes {
  /** Solar the house used itself: what the panels made, less what left it. */
  solarDirect: number[];
  batteryOut: number[];
  gridIn: number[];
  batteryIn: number[];
  gridOut: number[];
}

const magnitude = (watts: number | null | undefined): number =>
  watts === null || watts === undefined || !Number.isFinite(watts) ? 0 : Math.abs(watts);

/**
 * Solar the house consumed directly.
 *
 * Whatever the panels made, less whatever left the house by wire or went into
 * the battery. Counting all of it would credit the same watt twice: once as
 * solar arriving and again as battery discharge or grid export leaving.
 */
export const solarUsedDirectlyW = (row: PowerFlowQuarter): number => Math.max(
  0,
  magnitude(row.solarW) - magnitude(row.gridExportW) - magnitude(row.batteryChargeW),
);

export const powerFlowMagnitudes = (
  rows: readonly PowerFlowQuarter[],
): PowerFlowMagnitudes => ({
  solarDirect: rows.map(solarUsedDirectlyW),
  batteryOut: rows.map(row => magnitude(row.batteryDischargeW)),
  gridIn: rows.map(row => magnitude(row.gridImportW)),
  batteryIn: rows.map(row => magnitude(row.batteryChargeW)),
  gridOut: rows.map(row => magnitude(row.gridExportW)),
});
