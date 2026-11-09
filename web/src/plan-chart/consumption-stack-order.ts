// Shared chart policy. Keep the website and SHS app copies of this module identical.
interface StackSeries {
  key: string;
  values: readonly (number | null)[];
}

/** Device bands from bottom to top. Base load is added separately by the chart. */
export function orderConsumptionStack<T extends StackSeries>(
  series: readonly T[],
  samplesFor: (entry: T) => readonly (number | null | undefined)[] = entry => entry.values,
): T[] {
  return series.map(entry => {
    const samples = samplesFor(entry);
    let total = 0;
    let squares = 0;
    let movement = 0;
    let previous = 0;
    for (const [index, sample] of samples.entries()) {
      const watts = typeof sample === 'number' && Number.isFinite(sample) ? sample : 0;
      total += watts;
      squares += watts * watts;
      if (index > 0) movement += Math.abs(watts - previous);
      previous = watts;
    }
    // Off quarters increase relative spread. Repeated changes also move a
    // load up the stack, even when it runs more often than a single long run.
    const score = total > 0
      ? Math.sqrt(Math.max(0, samples.length * squares / total ** 2 - 1)) + movement / total
      : Infinity;
    return { entry, score };
  }).sort((a, b) => {
    const difference = a.score - b.score;
    if (difference) return difference;
    if (a.entry.key === b.entry.key) return 0;
    return a.entry.key < b.entry.key ? -1 : 1;
  }).map(({ entry }) => entry);
}
