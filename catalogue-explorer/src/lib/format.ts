/**
 * How a product row reads on screen. Extracted from the explore page because
 * the grid and the card view have to say the same thing about the same row: a
 * pot that reads "0.0065 kg / 6.5 g" in one place cannot read "0.0065 kg" in
 * the other, or the two views look like two different catalogues.
 */

import type { Product } from '$lib/catalogue';

export const count = (value: number) => value.toLocaleString('en-US');

/**
 * 237.0 reads as 237, 3785.41 as 3,785.41. Four decimals because the smallest
 * packs are published as fractions of a kilo (0.0065 kg) and rounding those
 * away would make two different products look like the same one.
 */
export function trim(value: number) {
	return value.toLocaleString('en-US', { maximumFractionDigits: 4 });
}

export function firing(row: Product) {
	if (row.cone_min || row.cone_max) {
		const range =
			row.cone_min && row.cone_max && row.cone_min !== row.cone_max
				? `${row.cone_min} to ${row.cone_max}`
				: (row.cone_max ?? row.cone_min);
		return `cone ${range}`;
	}
	if (row.max_celsius) {
		return row.min_celsius && row.min_celsius !== row.max_celsius
			? `${row.min_celsius} - ${row.max_celsius} C`
			: `${row.max_celsius} C`;
	}
	return null;
}

/** Units a metric reader takes at face value, so no conversion is added. */
const METRIC = new Set(['ml', 'cl', 'l', 'litre', 'liter', 'g', 'gr', 'gram', 'grams', 'kg']);

/**
 * How much is in the pot, as the supplier published it. The importer's metric
 * reading goes underneath - the same treatment a foreign-currency price gets -
 * whenever the published figure is hard to read at a glance: a pint or a pound
 * says little to a French potter, and neither does 0.0065 kg.
 */
export function size(row: Product) {
	if (row.size_value == null || !row.size_unit) return null;
	const normal = row.size_dimension === 'weight' ? row.size_g : row.size_ml;
	const awkward = !METRIC.has(row.size_unit.toLowerCase()) || row.size_value < 1;
	return {
		listed: `${trim(row.size_value)} ${row.size_unit}`,
		metric:
			awkward && normal != null && normal !== row.size_value
				? `${trim(normal)} ${row.size_dimension === 'weight' ? 'g' : 'ml'}`
				: null
	};
}

export function stock(row: Product) {
	if (!row.availability) return null;
	const inStock = row.availability.endsWith('InStock');
	const quantity = row.stock_quantity;
	return {
		inStock,
		label: inStock ? (quantity ? `in stock (${quantity})` : 'in stock') : 'out of stock'
	};
}

/** Converted figures are labelled everywhere they appear; see the page note. */
export function eur(value: number | null | undefined) {
	return value == null ? null : `${value.toFixed(2)} EUR`;
}

/** The price exactly as the shop published it, currency and pack size included. */
export function listedPrice(row: Product) {
	if (row.price == null) return null;
	const base = `${row.price.toFixed(2)} ${row.currency ?? ''}`.trim();
	const packed = row.quantity && row.unit ? ` / ${row.quantity} ${row.unit}` : '';
	return base + packed;
}

export function unitPrice(row: Product) {
	return row.unit_price_eur ? `${row.unit_price_eur.toFixed(2)} EUR/${row.unit_price_per}` : null;
}
