import { sql } from './db';

/**
 * Euro reference rates, from the ECB's daily file.
 *
 * These are reference rates, not transaction rates: a converted figure is
 * indicative and a bank will not fill an order at it. Every surface that shows
 * a converted price says so, and the original currency and amount stay visible
 * beside it.
 *
 * The file publishes one rate per currency as "units of that currency per
 * EUR", so a price converts with `amount / rate`.
 */
const ECB_DAILY = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml';

/** Rates last long enough that refetching per request would be rude. */
const TTL_MS = 6 * 60 * 60 * 1000;

export type Rates = {
	/** currency code -> units per EUR. EUR itself is 1. */
	rates: Record<string, number>;
	date: string | null;
	/** True when the network was unreachable and a stored file was used. */
	stale: boolean;
};

let cached: { at: number; value: Rates } | null = null;

/**
 * The last good response is kept in the database so a restart without network
 * still converts. It lives in the importer's own metadata table rather than in
 * a new one: this is retrieval provenance, which is what that table is for.
 */
async function remember(value: Rates) {
	await sql`
		insert into catalogue.sources (id, label, metadata)
		values ('ecb-fx', 'ECB euro foreign exchange reference rates',
		        ${sql.json({ ...value, saved_at: new Date().toISOString() })})
		on conflict (id) do update set metadata = excluded.metadata, updated_at = now()
	`;
}

async function recall(): Promise<Rates | null> {
	const [row] = await sql<{ metadata: Rates }[]>`
		select metadata from catalogue.sources where id = 'ecb-fx'
	`;
	if (!row?.metadata?.rates) return null;
	return { ...row.metadata, stale: true };
}

function parse(xml: string): Rates {
	const rates: Record<string, number> = { EUR: 1 };
	for (const match of xml.matchAll(/currency=['"]([A-Z]{3})['"]\s+rate=['"]([0-9.]+)['"]/g)) {
		const value = Number(match[2]);
		if (Number.isFinite(value) && value > 0) rates[match[1]] = value;
	}
	const date = xml.match(/time=['"](\d{4}-\d{2}-\d{2})['"]/)?.[1] ?? null;
	return { rates, date, stale: false };
}

export async function fxRates(): Promise<Rates> {
	if (cached && Date.now() - cached.at < TTL_MS) return cached.value;

	try {
		const response = await fetch(ECB_DAILY, { signal: AbortSignal.timeout(5000) });
		if (!response.ok) throw new Error(`ECB responded ${response.status}`);
		const value = parse(await response.text());
		if (Object.keys(value.rates).length < 2) throw new Error('no rates in ECB response');
		cached = { at: Date.now(), value };
		await remember(value).catch(() => {});
		return value;
	} catch {
		const stored = (await recall().catch(() => null)) ?? { rates: { EUR: 1 }, date: null, stale: true };
		cached = { at: Date.now(), value: stored };
		return stored;
	}
}

/**
 * The row's own rate, as a SQL expression: the rate table travels into the
 * query as one jsonb parameter and is looked up per row by its `currency`
 * column. Conversion then happens where the aggregation happens, so a USD
 * offer takes part in a median or a cheapest-of comparison instead of being
 * dropped from it. A currency the ECB does not publish yields null, which the
 * `is not null` guards already exclude.
 */
export function eurRate(rates: Rates) {
	return sql`nullif((${sql.json(rates.rates)}::jsonb ->> currency)::numeric, 0)`;
}

export function toEur(amount: number | null, currency: string | null, rates: Rates) {
	if (amount == null || !currency) return null;
	const rate = rates.rates[currency.toUpperCase()];
	return rate ? amount / rate : null;
}
