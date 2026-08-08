import type { PageServerLoad } from './$types';
import { ControlError, configured, get } from '$lib/server/control';
import type { Job, LogLine } from '$lib/ops/types';

export const load: PageServerLoad = async ({ params, url }) => {
	if (!configured()) return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	const level = url.searchParams.get('level');
	const search = url.searchParams.get('q');
	const query = new URLSearchParams({ limit: '1000' });
	if (level) query.set('level', level);
	if (search) query.set('q', search);

	try {
		const [job, logs] = await Promise.all([
			get<{ job: Job }>(`/v1/jobs/${params.jobId}`),
			get<{ lines: LogLine[]; next_after: number | null }>(
				`/v1/jobs/${params.jobId}/logs?${query}`
			)
		]);
		return { job: job.job, lines: logs.lines, level, search };
	} catch (error) {
		return { unavailable: error instanceof ControlError ? error.message : String(error) };
	}
};
