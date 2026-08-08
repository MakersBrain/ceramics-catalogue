import type { ColDef } from 'ag-grid-community';
import type { ColumnKey } from '$lib/columns';
import { COLUMNS } from '$lib/columns';
import { countryName } from '$lib/countries';
import { eur, firing, listedPrice, size, stock, unitPrice } from '$lib/format';
import type { Product, Sort } from '$lib/catalogue';

/**
 * The grid's view of the column set defined in $lib/columns.
 *
 * Widths live here rather than being left to the grid to guess, because the
 * columns hold known things: a country is two letters and a product name is a
 * sentence, and letting both settle on the same width wastes the row. Only the
 * name column flexes, so growing the window lengthens the readable field
 * instead of stretching fifteen columns of whitespace.
 *
 * Where the old table stacked a second line under a cell - the metric reading
 * of an awkward pack size, the price as the shop listed it - the grid puts that
 * on the cell's tooltip instead. Rows are one line high in a spreadsheet, and a
 * row that grows to two whenever a supplier prices in GBP makes the whole sheet
 * ripple.
 */

/** '-' rather than an empty cell: a blank reads as "not loaded yet". */
const MISSING = '-';

type Cell = {
	/** What the cell says, and what a copy of the selection puts on the clipboard. */
	text: (row: Product) => string | null;
	/** The fuller reading, shown on hover. */
	hint?: (row: Product) => string | null;
	width: number;
	flex?: number;
	minWidth?: number;
	right?: boolean;
};

const CELLS: Record<ColumnKey, Cell> = {
	// The one column that flexes, and the one with a real floor under it: a
	// product name clipped to twenty characters identifies nothing, and these
	// catalogues name a glaze with a sentence.
	name: {
		text: (row) => row.name,
		hint: (row) => row.name,
		width: 320,
		flex: 1,
		minWidth: 300,
	},
	code: { text: (row) => row.code, width: 110 },
	supplier: { text: (row) => row.supplier_label, width: 150 },
	country: {
		text: (row) => (row.country ? countryName(row.country) : null),
		width: 120,
	},
	brand: { text: (row) => row.brand, width: 120 },
	family: { text: (row) => row.family, width: 120 },
	colour: { text: (row) => row.colour, width: 130 },
	surface: { text: (row) => row.surface, width: 110 },
	firing: { text: firing, width: 130 },
	application: {
		text: (row) => row.application_methods?.join(', ') ?? null,
		width: 150,
	},
	size: {
		text: (row) => size(row)?.listed ?? null,
		// The importer's metric reading, kept off the face of the cell but one
		// hover away, exactly as the two-line table had it.
		hint: (row) => {
			const parsed = size(row);
			return parsed?.metric ? `${parsed.listed} (${parsed.metric})` : (parsed?.listed ?? null);
		},
		width: 110,
		right: true,
	},
	form: {
		text: (row) => (row.form === 'powder' ? 'dry mix' : row.form),
		width: 90,
	},
	stock: { text: (row) => stock(row)?.label ?? null, width: 130 },
	price: {
		text: (row) => eur(row.price_eur),
		// A converted figure is never shown without the figure it came from.
		hint: (row) =>
			row.currency && row.currency !== 'EUR'
				? `${eur(row.price_eur)} - listed ${listedPrice(row)}`
				: listedPrice(row),
		width: 120,
		right: true,
	},
	unit_price: { text: unitPrice, width: 140, right: true },
};

/**
 * Stock is the one cell that carries a state, so it gets a glyph and a colour
 * on top of the words - never the colour alone, which would put the whole
 * distinction out of reach of a reader who cannot see it.
 */
function stockCell(row: Product | undefined) {
	if (!row) return '';
	const state = stock(row);
	if (!state) return MISSING;
	const colour = state.inStock ? 'var(--good)' : 'var(--text-muted)';
	const glyph = state.inStock ? '●' : '○';
	return `<span style="color: ${colour}"><span aria-hidden="true">${glyph}</span> ${state.label}</span>`;
}

/**
 * The two cells that are links: the product on its own storefront, and the
 * manufacturer's code as a jump into the comparison page. Both stop the click
 * from reaching the row, which would otherwise open the detail panel on top of
 * the page the reader just asked for.
 */
function linkCell(href: string, label: string, colour: string) {
	const anchor = document.createElement('a');
	anchor.href = href;
	anchor.textContent = label;
	anchor.style.color = colour;
	anchor.onclick = (event) => event.stopPropagation();
	return anchor;
}

/**
 * @param sort which column starts out sorted, and which way. Declared on the
 * column rather than applied to the grid afterwards: applying it after the grid
 * exists means it sorts once with the default order, asks the server for that
 * block, then sorts again - two requests for one view, and the answer to the
 * first can land after the second and leave the sheet showing empty rows.
 */
export function productColumns(chosen: readonly ColumnKey[], sort: Sort): ColDef<Product>[] {
	// Mapped over the chosen list rather than filtered out of COLUMNS, so the
	// sheet is laid out in the reader's order and not in this file's.
	return chosen
		.map((key) => COLUMNS.find((column) => column.key === key))
		.filter((column) => column !== undefined)
		.map((column) => {
			const cell = CELLS[column.key];
			const base: ColDef<Product> = {
				colId: column.key,
				headerName: column.label,
				// The sort key the server understands is the column id, so a column that
				// can be shown can always be sorted by.
				field: column.key as keyof Product & string,
				sortable: true,
				sort: column.key === sort.key ? sort.dir : null,
				resizable: true,
				width: cell.width,
				flex: cell.flex,
				minWidth: cell.minWidth,
				// Values are read through the cell table rather than off the row, so the
				// clipboard gets what the eye got.
				valueGetter: (params) => (params.data ? cell.text(params.data) : null),
				valueFormatter: (params) => params.value ?? MISSING,
				tooltipValueGetter: (params) =>
					params.data ? (cell.hint?.(params.data) ?? cell.text(params.data)) : null,
				cellClass: cell.right ? 'text-right tabular-nums' : undefined,
				headerClass: cell.right ? 'ag-right-aligned-header' : undefined,
			};

			if (column.key === 'name') {
				return {
					...base,
					cellRenderer: (params: { data?: Product; value?: string }) =>
						params.data ? linkCell(params.data.url, params.value ?? '', 'var(--text-primary)') : '',
				};
			}
			if (column.key === 'code') {
				return {
					...base,
					cellRenderer: (params: { data?: Product; value?: string }) =>
						params.data?.code
							? linkCell(
									`/compare?q=${encodeURIComponent(params.data.code)}`,
									params.data.code,
									'var(--accent)',
								)
							: MISSING,
				};
			}
			if (column.key === 'stock') {
				return {
					...base,
					cellRenderer: (params: { data?: Product }) => stockCell(params.data),
				};
			}
			return base;
		});
}
