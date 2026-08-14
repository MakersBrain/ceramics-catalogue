import {
	BANDS,
	band,
	brands,
	families,
	familyMix,
	medianUnitPrice,
	overview,
	supplierCoverage,
	widestSpread
} from '$lib/server/queries';
import { fxRates } from '$lib/server/fx';
import { stable } from '$lib/server/cache';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ url }) => {
	const bandId = band(url.searchParams.get('band') ?? 'medium').id;
	const brand = url.searchParams.get('brand')?.trim() || null;
	const family = url.searchParams.get('family')?.trim() || null;

	// The filter row scopes every panel below it, so the numbers always agree.
	// The one exception is the family mix, which is the panel that shows what the
	// product types are; see familyMix.
	const rates = await fxRates();
	const build = () => Promise.all([
		overview(brand, family),
		supplierCoverage(brand, family),
		familyMix(brand),
		// The price panels compare like with like, so they always sit inside one
		// product type. Glaze is the default because it is the type most suppliers
		// publish a unit price for; choosing a type moves both panels to it.
		medianUnitPrice(bandId, brand, family ?? 'glaze', 5, rates),
		widestSpread(bandId, brand, family, 12, 3, rates),
		brands(),
		families()
	]);
	const [totals, suppliers, mix, medians, spread, brandOptions, familyOptions] =
		brand === null && family === null
			? await stable(`homepage:${bandId}`, build)
			: await build();

	return {
		totals,
		suppliers,
		families: mix,
		medians,
		spread,
		bandId,
		bands: BANDS,
		brand,
		brandOptions,
		family,
		familyOptions,
		/** What the two price panels are actually about, chosen or defaulted. */
		pricedFamily: family ?? 'glaze',
		fx: { date: rates.date, stale: rates.stale }
	};
};
