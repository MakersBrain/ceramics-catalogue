import { readColumns } from '$lib/columns';
import { readSort } from '$lib/catalogue';
import { facets, products, readFilters } from '$lib/server/explore';
import { fxRates } from '$lib/server/fx';
import type { PageServerLoad } from './$types';

const PAGE_SIZE = 48;

export const load: PageServerLoad = async ({ url }) => {
	const filters = readFilters(url.searchParams);
	const page = Math.max(1, Number(url.searchParams.get('page') ?? 1) || 1);

	// View state travels in the URL alongside the filters, so a sorted table of
	// a filtered selection is one link.
	const view = url.searchParams.get('view') === 'table' ? 'table' : 'grid';
	const columns = readColumns(url.searchParams);
	const sort = {
		key: readSort(url.searchParams.get('sort')),
		dir: url.searchParams.get('dir') === 'desc' ? ('desc' as const) : ('asc' as const)
	};

	// One rate lookup per request, shared by every query below it.
	const rates = await fxRates();
	const [options, found] = await Promise.all([
		facets(filters),
		products(filters, PAGE_SIZE, (page - 1) * PAGE_SIZE, sort, rates)
	]);

	return {
		filters,
		facets: options,
		rows: found.rows,
		total: found.total,
		page,
		pageSize: PAGE_SIZE,
		view,
		columns,
		sort,
		fx: { date: rates.date, stale: rates.stale }
	};
};
