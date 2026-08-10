import type { RequestHandler } from './$types';
import { ControlError, get } from '$lib/server/control';
import type { LogPage } from '$lib/ops/types';

/** Browser-safe proxy for the on-demand job trace; the control token stays server-side. */
export const GET: RequestHandler = async ({ params, url }) => {
	const query = new URLSearchParams({
		after: url.searchParams.get('after') ?? '0',
		limit: url.searchParams.get('limit') ?? '100'
	});
	try {
		const page = await get<LogPage>(`/v1/jobs/${params.id}/logs?${query}`);
		return Response.json(page, { headers: { 'cache-control': 'no-store' } });
	} catch (error) {
		const status = error instanceof ControlError && error.status === 400 ? 400 : 502;
		return Response.json(
			{ error: error instanceof Error ? error.message : String(error) },
			{ status }
		);
	}
};
