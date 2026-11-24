// Shared chart policy. Keep the website and SHS app copies of this module identical.
interface StackSeries {
  key: string;
  values: readonly (number | null)[];
}

/**
 * Device bands from bottom to top. Base load is added separately by the chart.
 *
 * Continuous loads go to the bottom, and among similarly continuous loads the
 * more powerful one goes lower. Continuity is how much the band's edge moves
 * per unit of energy: a load that runs in one long block moves its edge twice,
 * however long it then stays off, so idle time never lifts it. Dividing by the
 * square root of the running power puts big loads lower without letting power
 * outrank continuity. The rule has no tuning constant: scaling every load by
 * the same factor leaves the order unchanged.
 */
export function orderConsumptionStack<T extends StackSeries>(
  series: readonly T[],
  samplesFor: (entry: T) => readonly (number | null | undefined)[] = entry => entry.values,
): T[] {
  return series.map(entry => {
    const samples = samplesFor(entry);
    let total = 0;
    let running = 0;
    let movement = 0;
    let previous = 0;
    for (const [index, sample] of samples.entries()) {
      const watts = typeof sample === 'number' && Number.isFinite(sample) ? sample : 0;
      total += watts;
      if (watts > 0) running += 1;
      if (index > 0) movement += Math.abs(watts - previous);
      previous = watts;
    }
    const score = total > 0 ? movement / total / Math.sqrt(total / running) : Infinity;
    return { entry, score };
  }).sort((a, b) => {
    const difference = a.score - b.score;
    if (difference) return difference;
    if (a.entry.key === b.entry.key) return 0;
    return a.entry.key < b.entry.key ? -1 : 1;
  }).map(({ entry }) => entry);
}
