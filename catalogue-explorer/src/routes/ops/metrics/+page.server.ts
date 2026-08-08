import type { PageServerLoad } from './$types';
import {
	errorRateByHost,
	offersPerDay,
	recordsPerSource,
	runsOverTime,
	stalenessBands
} from '$lib/server/ops';

export const load: PageServerLoad = async () => {
	try {
		const [runs, sources, hosts, staleness, offers] = await Promise.all([
			runsOverTime(),
			recordsPerSource(),
			errorRateByHost(),
			stalenessBands(),
			offersPerDay()
		]);
		return { runs, sources, hosts, staleness, offers };
	} catch (error) {
		// The ops tables may not exist yet on a database that predates the ops
		// schema; that is a message, not a stack trace.
		return { unavailable: error instanceof Error ? error.message : String(error) };
	}
};
