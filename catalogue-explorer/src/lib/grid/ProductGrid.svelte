<script lang="ts">
	import {
		AllCommunityModule,
		ModuleRegistry,
		createGrid,
		type GridApi,
		type IGetRowsParams
	} from 'ag-grid-community';
	import { untrack } from 'svelte';
	import type { ColumnKey } from '$lib/columns';
	import { productColumns } from '$lib/grid/productColumns';
	import { gridTheme } from '$lib/grid/theme';
	import type { Product, Sort, SortKey } from '$lib/catalogue';

	/**
	 * The catalogue as one continuous sheet.
	 *
	 * Rows arrive a block at a time from /explore/rows, so scrolling through
	 * 27,000 products neither ships 27,000 rows to the browser nor makes the
	 * reader click through 560 pages. The grid asks for the range it is about to
	 * draw and the database answers with exactly that range.
	 *
	 * Sorting stays on the server for the same reason: only a block of the
	 * selection is in the browser at any moment, so a client-side sort would
	 * order the wrong thing. Every sort is a new set of blocks.
	 */

	ModuleRegistry.registerModules([AllCommunityModule]);

	let {
		columns,
		/** The filter query string, exactly as the page's own URL carries it. */
		query,
		total,
		sort,
		/** How wide the sheet is, so the name column's floor can give way to a
		    phone rather than pushing the price beyond the edge. */
		width,
		/** Told when the reader sorts, so the URL keeps matching what is on screen. */
		onSort,
		/** Told when the reader drags a column somewhere else. */
		onArrange,
		/** Told when a row is opened. */
		onOpen
	}: {
		columns: readonly ColumnKey[];
		query: string;
		total: number;
		sort: Sort;
		width: number;
		onSort: (sort: Sort) => void;
		onArrange: (columns: ColumnKey[]) => void;
		onOpen: (row: Product) => void;
	} = $props();

	let host: HTMLDivElement;
	let api = $state<GridApi<Product>>();

	/**
	 * A block request carries the page's filters unchanged and adds the range and
	 * the sort. The sort is read off the grid rather than off the prop: the grid
	 * has already applied it by the time it asks for rows, and reading it back
	 * from the URL would fetch one block ordered the old way.
	 */
	async function fetchBlock(params: IGetRowsParams) {
		const search = new URLSearchParams(query);
		search.set('start', String(params.startRow));
		search.set('end', String(params.endRow));
		const [first] = params.sortModel;
		if (first) {
			search.set('sort', first.colId);
			search.set('dir', first.sort);
		}
		try {
			const response = await fetch(`/explore/rows?${search}`);
			if (!response.ok) throw new Error(`rows: ${response.status}`);
			const { rows } = (await response.json()) as { rows: Product[] };
			// A short block is the end of the selection. Saying so stops the grid
			// asking for the block after it.
			const last = rows.length < params.endRow - params.startRow ? params.startRow + rows.length : undefined;
			params.successCallback(rows, last);
		} catch {
			params.failCallback();
		}
	}

	function mount(node: HTMLDivElement) {
		api = createGrid<Product>(node, {
			theme: gridTheme,
			columnDefs: productColumns(columns, sort, width),
			defaultColDef: {
				// One sort at a time, matching what the URL can carry and what the
				// server's ORDER BY allows.
				sortingOrder: ['asc', 'desc'],
				suppressHeaderMenuButton: true
			},
			rowModelType: 'infinite',
			// 100 rows a request. Large enough that an ordinary scroll rarely waits,
			// small enough that a jump to the middle of the catalogue is one query.
			cacheBlockSize: 100,
			// Roughly two screens of blocks are kept; the rest are dropped and
			// re-fetched if the reader comes back to them.
			maxBlocksInCache: 10,
			datasource: { rowCount: total, getRows: fetchBlock },
			// The row count is known from the page load, so the scrollbar is the
			// right length from the first paint rather than growing as blocks land.
			infiniteInitialRowCount: total,
			rowSelection: { mode: 'singleRow', checkboxes: false, enableClickSelection: true },
			// Native tooltips: the second line the old table stacked under a cell is
			// on the hover, and it should behave like every other tooltip on the OS.
			enableBrowserTooltips: true,
			suppressCellFocus: false,
			onSortChanged: (event) => {
				const [first] = event.api.getColumnState().filter((state) => state.sort);
				onSort(
					first?.colId
						? { key: first.colId as SortKey, dir: first.sort === 'desc' ? 'desc' : 'asc' }
						: { key: 'name', dir: 'asc' }
				);
			},
			/**
			 * Reported when the drag finishes rather than on every column-moved
			 * event, which fires continuously as the header slides under the
			 * pointer: the reader's arrangement is where they let go, not every
			 * position they passed through on the way.
			 */
			onDragStopped: (event) => {
				const order = event.api
					.getColumnState()
					.map((state) => state.colId as ColumnKey)
					.filter((key) => columns.includes(key));
				if (order.join(',') !== columns.join(',')) onArrange(order);
			},
			onRowClicked: (event) => {
				if (event.data) onOpen(event.data);
			}
		});

		return { destroy: () => api?.destroy() };
	}

	/**
	 * A column set is a different sheet, not different data: swap the definitions
	 * in place rather than tearing the grid down and losing the scroll position.
	 *
	 * The width is tracked alongside the column set, because turning a phone on
	 * its side changes what the name column's floor should be as surely as adding
	 * a column does.
	 *
	 * The sort is read back off the grid rather than off the prop, and untracked,
	 * so that adding a column keeps whatever the reader had sorted by and so that
	 * sorting does not itself rebuild the columns.
	 */
	$effect(() => {
		const next = columns;
		const room = width;
		untrack(() => {
			if (!api) return;
			const live = api.getColumnState().find((state) => state.sort);
			api.setGridOption(
				'columnDefs',
				productColumns(
					next,
					live?.colId
						? { key: live.colId as SortKey, dir: live.sort === 'desc' ? 'desc' : 'asc' }
						: sort,
					room
				)
			);
		});
	});
</script>

<div class="ag-sheet h-full w-full" use:mount bind:this={host}></div>

<style>
	/* The grid draws its own borders, so the wrapper adds none. What it does add
	   is the guarantee that the sheet fills whatever box the page gives it -
	   AG Grid measures its viewport, and a host with no height renders nothing. */
	.ag-sheet {
		min-height: 0;
	}

	/* A row is a link to its own detail, and says so on hover. */
	.ag-sheet :global(.ag-row) {
		cursor: pointer;
	}

	/* Header labels wrap to nothing useful at these widths; one line, clipped. */
	.ag-sheet :global(.ag-header-cell-text) {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
