import type { PageServerLoad } from './$types';
import { ControlError, configured, get } from '$lib/server/control';
import type { RunRow } from '$lib/ops/types';

export const load: PageServerLoad = async ({ url }) => {
	if (!configured()) return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	const cursor = url.searchParams.get('cursor');
	try {
		return await get<{ runs: RunRow[]; next_cursor: string | null }>(
			`/v1/runs?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`
		);
	} catch (error) {
		return { unavailable: error instanceof ControlError ? error.message : String(error) };
	}
};
