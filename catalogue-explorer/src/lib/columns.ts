/**
 * The table's column set, chosen by the reader and carried in the URL like
 * every other piece of view state on /explore. Shared between the loader (which
 * validates the `col` params) and the page (which renders headers and cells),
 * so a key can never exist in one and not the other.
 */

/**
 * `priority` is the rung of the ladder in `fit` at which a column appears, and
 * the rungs are set from what the columns actually measure rather than from the
 * usual breakpoint names: the sheet either fits or it scrolls sideways, and
 * "tablet" is not a width. The order the rungs fill in is what a reader asks for
 * when a table is too narrow - what is it, what does it cost, who has it, can I
 * buy it, then the identifiers, then everything else.
 *
 * It is a separate judgement from `default`, which is about what is worth
 * showing at all: unit price is on by default and last to survive a squeeze,
 * because it is the most useful column here and also the widest.
 */
export const COLUMNS = [
	{ key: 'name', label: 'Product', align: 'left', priority: 1, default: true },
	{ key: 'code', label: 'Code', align: 'left', priority: 4, default: true },
	{ key: 'supplier', label: 'Supplier', align: 'left', priority: 3, default: true },
	{ key: 'country', label: 'Country', align: 'left', priority: 5, default: false },
	{ key: 'brand', label: 'Brand', align: 'left', priority: 5, default: false },
	{ key: 'family', label: 'Type', align: 'left', priority: 5, default: false },
	{ key: 'colour', label: 'Colour', align: 'left', priority: 5, default: true },
	{ key: 'surface', label: 'Surface', align: 'left', priority: 5, default: false },
	{ key: 'firing', label: 'Firing', align: 'left', priority: 5, default: false },
	{ key: 'application', label: 'Application', align: 'left', priority: 5, default: false },
	{ key: 'size', label: 'Size', align: 'left', priority: 4, default: true },
	{ key: 'form', label: 'Form', align: 'left', priority: 5, default: false },
	{ key: 'stock', label: 'Stock', align: 'left', priority: 2, default: true },
	{ key: 'price', label: 'Price (EUR)', align: 'right', priority: 1, default: true },
	{ key: 'unit_price', label: 'Unit price (EUR)', align: 'right', priority: 5, default: true },
] as const;

export type ColumnKey = (typeof COLUMNS)[number]['key'];

export const COLUMN_KEYS = COLUMNS.map((column) => column.key) as ColumnKey[];

/**
 * The eight columns that answer the question the catalogue is usually opened
 * with: what is it, who sells it, what pack, and what does it cost.
 *
 * This used to be all fifteen, on the reasoning that a reader can always hide
 * what they do not want. In practice fifteen columns is three screens of
 * sideways scrolling on a laptop and the price - the column most people came
 * for - starts off screen. The seven that are off by default are the ones that
 * are either usually empty (surface, firing, application), a restatement of
 * something already shown (form restates size, family restates the name), or
 * only interesting once a filter has already been set on them (country, brand).
 * All seven are one tick away in the column picker, and the choice is
 * remembered.
 */
export const DEFAULT_COLUMNS = COLUMNS.filter((column) => column.default).map(
	(column) => column.key
) as ColumnKey[];

/**
 * The chosen columns that a viewport this wide can actually show.
 *
 * A sheet is the one thing on this site that cannot simply reflow: fifteen
 * columns on a phone means the name column fills the screen and everything
 * else is somewhere off to the right, reachable only by a horizontal scroll
 * that no scrollbar advertises. So the sheet drops the low-priority columns
 * rather than pretending they are readable.
 *
 * This narrowing is display-only. The reader's arrangement is untouched - it
 * stays in the URL and in the store, and it comes straight back when the
 * window is widened or the phone is turned on its side.
 */
export function fit(chosen: readonly ColumnKey[], width: number): ColumnKey[] {
	// Each threshold is the width at which that rung's columns add up to less than
	// the viewport, given the pixel widths declared in $lib/grid/productColumns and
	// the 300px the product name wants. Five rungs rather than three, because
	// with three the step from "name and price" to six columns fell across the
	// tablet widths, and a 700px window got two columns while an 800px one got a
	// sheet that scrolled sideways.
	const budget =
		width >= 1280 ? 5 : width >= 980 ? 4 : width >= 760 ? 3 : width >= 560 ? 2 : 1;
	const kept = chosen.filter(
		(key) => (COLUMNS.find((column) => column.key === key)?.priority ?? 3) <= budget
	);
	// Never hand the grid an empty column set: a reader who has hidden the name
	// and the price would otherwise get a blank sheet on a phone.
	return kept.length ? kept : chosen.slice(0, 1);
}

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

/**
 * A drag on a narrowed sheet, folded back into the whole arrangement.
 *
 * The sheet only knows about the columns it was given, so when the reader drags
 * one on a phone the order that comes back is missing everything `fit` dropped.
 * Taking it as the new arrangement would quietly delete those columns the first
 * time anyone rearranged a table on a small screen.
 *
 * A dropped column was never on screen, so the reader cannot have meant to move
 * it: each one stays attached to the visible column it used to follow, and any
 * that led the list stay in front of it.
 */
export function merge(
	full: readonly ColumnKey[],
	reordered: readonly ColumnKey[]
): ColumnKey[] {
	const trailing = new Map<ColumnKey | null, ColumnKey[]>();
	let anchor: ColumnKey | null = null;
	for (const key of full) {
		if (reordered.includes(key)) {
			anchor = key;
		} else {
			trailing.set(anchor, [...(trailing.get(anchor) ?? []), key]);
		}
	}
	return [
		...(trailing.get(null) ?? []),
		...reordered.flatMap((key) => [key, ...(trailing.get(key) ?? [])])
	];
}

export function isDefaultColumns(columns: readonly ColumnKey[]) {
	return columns.join(',') === DEFAULT_COLUMNS.join(',');
}
