/**
 * The table's column set, chosen by the reader and carried in the URL like
 * every other piece of view state on /explore. Shared between the loader (which
 * validates the `col` params) and the page (which renders headers and cells),
 * so a key can never exist in one and not the other.
 */

export const COLUMNS = [
	{ key: 'name', label: 'Product', align: 'left' },
	{ key: 'code', label: 'Code', align: 'left' },
	{ key: 'supplier', label: 'Supplier', align: 'left' },
	{ key: 'country', label: 'Country', align: 'left' },
	{ key: 'brand', label: 'Brand', align: 'left' },
	{ key: 'family', label: 'Type', align: 'left' },
	{ key: 'colour', label: 'Colour', align: 'left' },
	{ key: 'surface', label: 'Surface', align: 'left' },
	{ key: 'firing', label: 'Firing', align: 'left' },
	{ key: 'application', label: 'Application', align: 'left' },
	{ key: 'size', label: 'Size', align: 'left' },
	{ key: 'form', label: 'Form', align: 'left' },
	{ key: 'stock', label: 'Stock', align: 'left' },
	{ key: 'price', label: 'Price (EUR)', align: 'right' },
	{ key: 'unit_price', label: 'Unit price (EUR)', align: 'right' },
] as const;

export type ColumnKey = (typeof COLUMNS)[number]['key'];

export const COLUMN_KEYS = COLUMNS.map((column) => column.key) as ColumnKey[];

/**
 * Everything except form, which for most products only restates what the size
 * already says (a volume is a ready-to-use liquid, a mass is a dry mix).
 */
export const DEFAULT_COLUMNS = COLUMN_KEYS.filter((key) => key !== 'form');

/**
 * The chosen columns, in the order they are to be shown.
 *
 * The order is the URL's, not this file's. It used to be forced back into
 * COLUMNS order on the grounds that the picker offers a set rather than an
 * arrangement - true of the picker, but no longer true of the sheet, where a
 * column can be dragged where the reader wants it. Dropping a column back to
 * its declared position the moment anything else on the page changed would make
 * that drag look like it had not taken.
 *
 * A Set keeps the first mention of a repeated key and drops the rest, so a
 * hand-edited URL cannot ask for the same column twice.
 */
export function readColumns(params: URLSearchParams): ColumnKey[] {
	// Accepts either repeated params (?col=name&col=code) or one comma-separated
	// list, so a hand-edited URL works either way.
	const chosen = new Set<ColumnKey>();
	for (const raw of params.getAll('col')) {
		for (const part of raw.split(',')) {
			const key = part.trim() as ColumnKey;
			if (COLUMN_KEYS.includes(key)) chosen.add(key);
		}
	}
	// Unchecking everything would leave nothing to read; treat it as a reset.
	return chosen.size ? [...chosen] : DEFAULT_COLUMNS;
}

/**
 * Every column, shown ones first in the order they are shown, then the hidden
 * ones in their declared order. This is the order the picker lists them in, and
 * because a GET form submits its checkboxes in document order, ticking a hidden
 * column appends it to the arrangement rather than teleporting it into the
 * middle of one the reader has already arranged.
 */
export function arrange(chosen: readonly ColumnKey[]) {
	const shown = chosen
		.map((key) => COLUMNS.find((column) => column.key === key))
		.filter((column) => column !== undefined);
	return [...shown, ...COLUMNS.filter((column) => !chosen.includes(column.key))];
}

export function isDefaultColumns(columns: readonly ColumnKey[]) {
	return columns.join(',') === DEFAULT_COLUMNS.join(',');
}
