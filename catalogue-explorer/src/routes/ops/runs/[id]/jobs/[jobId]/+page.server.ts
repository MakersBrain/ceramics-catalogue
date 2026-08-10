import type { PageServerLoad } from './$types';
import { ControlError, configured, get } from '$lib/server/control';
import type { Job, JobChanges, LogLine } from '$lib/ops/types';

export const load: PageServerLoad = async ({ params, url }) => {
	if (!configured()) return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	const level = url.searchParams.get('level');
	const search = url.searchParams.get('q');
	const changeKind = url.searchParams.get('change_kind');
	const changeSearch = url.searchParams.get('change_q');
	const query = new URLSearchParams({ limit: '1000' });
	if (level) query.set('level', level);
	if (search) query.set('q', search);

	try {
		const changesQuery = new URLSearchParams({ limit: '200' });
		if (changeKind) changesQuery.set('kind', changeKind);
		if (changeSearch) changesQuery.set('q', changeSearch);
		const [job, logs, changesResult] = await Promise.all([
			get<{ job: Job }>(`/v1/jobs/${params.jobId}`),
			get<{ lines: LogLine[]; next_after: number | null }>(
				`/v1/jobs/${params.jobId}/logs?${query}`
			),
			get<JobChanges>(`/v1/jobs/${params.jobId}/changes?${changesQuery}`)
				.then((changes) => ({ changes }))
				.catch((error) => ({
					changesUnavailable: error instanceof ControlError ? error.message : String(error)
				}))
		]);
		return {
			job: job.job,
			lines: logs.lines,
			level,
			search,
			changeKind,
			changeSearch,
			...changesResult
		};
	} catch (error) {
		return { unavailable: error instanceof ControlError ? error.message : String(error) };
	}
};
