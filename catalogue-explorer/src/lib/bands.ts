/**
 * Pack-size bands, in litres.
 *
 * A 59 ml jar always costs more per litre than a 473 ml pot, so every
 * supplier-to-supplier comparison in this app happens inside one band. Shared
 * between the SQL that aggregates and the pages that group offers, so the two
 * never drift apart.
 */
export type Band = { id: string; label: string; low: number; high: number };

export const BANDS: Band[] = [
	{ id: 'small', label: 'under 150 ml', low: 0, high: 0.15 },
	{ id: 'medium', label: '150 ml - 600 ml', low: 0.15, high: 0.6 },
	{ id: 'large', label: '0.6 L - 2 L', low: 0.6, high: 2 },
	{ id: 'bulk', label: 'over 2 L', low: 2, high: 1e9 }
];

export function band(id: string): Band {
	return BANDS.find((entry) => entry.id === id) ?? BANDS[1];
}

export function bandOf(litres: number | null | undefined): Band | null {
	if (litres == null) return null;
	return BANDS.find((entry) => litres >= entry.low && litres < entry.high) ?? null;
}
