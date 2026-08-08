import { json } from '@sveltejs/kit';
import { readSort } from '$lib/catalogue';
import { productRows, readFilters } from '$lib/server/explore';
import { fxRates } from '$lib/server/fx';
import type { RequestHandler } from './$types';

/**
 * One block of rows for the infinite-scroll table.
 *
 * The query string is the same one /explore reads, so a block request is the
 * page's own URL with a row range bolted on: whatever the filters mean on the
 * page they mean here, and there is no second copy of the filter parsing to
 * drift out of step.
 *
 * No total is returned. The page load has already counted the selection, and
 * scrolling does not change it.
 */

/** A ceiling on what one scroll can ask the database for. */
const MAX_BLOCK = 500;

export const GET: RequestHandler = async ({ url }) => {
	const filters = readFilters(url.searchParams);
	const start = Math.max(0, Number(url.searchParams.get('start') ?? 0) || 0);
	const end = Number(url.searchParams.get('end') ?? 0) || 0;
	const limit = Math.min(MAX_BLOCK, Math.max(1, end - start));
	const sort = {
		key: readSort(url.searchParams.get('sort')),
		dir: url.searchParams.get('dir') === 'desc' ? ('desc' as const) : ('asc' as const)
	};

	const rows = await productRows(filters, limit, start, sort, await fxRates());
	return json({ rows });
};
