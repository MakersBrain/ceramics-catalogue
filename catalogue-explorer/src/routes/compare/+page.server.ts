import { fxRates } from '$lib/server/fx';
import { searchOffers, type Offer } from '$lib/server/queries';
import type { PageServerLoad } from './$types';

export type Group = {
	code: string;
	name: string;
	family: string | null;
	suppliers: number;
	offers: Offer[];
};

export const load: PageServerLoad = async ({ url }) => {
	const query = url.searchParams.get('q') ?? '';
	const rates = await fxRates();
	const offers = await searchOffers(query, 200, rates);

	// Group by manufacturer code — the only key that means "the same product"
	// across two storefronts. Sorting is by comparable unit price where there
	// is one, so the cheapest offer of a group is always its first row.
	const groups = new Map<string, Group>();
	for (const offer of offers) {
		let group = groups.get(offer.code);
		if (!group) {
			group = {
				code: offer.code,
				name: offer.name,
				family: offer.family,
				suppliers: 0,
				offers: []
			};
			groups.set(offer.code, group);
		}
		group.offers.push(offer);
	}

	const ranked = [...groups.values()]
		.map((group) => ({
			...group,
			suppliers: new Set(group.offers.map((offer) => offer.supplier)).size
		}))
		.sort((a, b) => b.suppliers - a.suppliers || a.code.localeCompare(b.code))
		.slice(0, 12);

	return {
		query,
		groups: ranked,
		total: offers.length,
		fx: { date: rates.date, stale: rates.stale }
	};
};
