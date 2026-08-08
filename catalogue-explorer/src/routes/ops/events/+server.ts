import type { RequestHandler } from './$types';
import { stream } from '$lib/server/control';

/**
 * The SSE proxy.
 *
 * `EventSource` cannot set request headers, so a bearer token cannot be
 * attached by the browser. Rather than accepting a query-string token — which
 * lands in access logs and in `Referer` headers — the browser connects here,
 * this route is authenticated by the session, and the upstream stream is opened
 * with the token server-side. The token never reaches the browser and
 * catalogue-control stays unpublished.
 *
 * The upstream body is passed through untouched. Reading it to transform it
 * would buffer a connection that is meant to stay open for hours.
 */
export const GET: RequestHandler = async ({ url, request }) => {
	const query = url.search || '';
	const upstream = await stream(query, request.headers.get('last-event-id'));

	if (!upstream.ok || !upstream.body) {
		return new Response(`upstream stream unavailable (${upstream.status})`, {
			status: upstream.status === 401 ? 500 : 502
		});
	}

	return new Response(upstream.body, {
		headers: {
			'content-type': 'text/event-stream',
			'cache-control': 'no-cache, no-transform',
			connection: 'keep-alive',
			// Caddy additionally needs `flush_interval -1` on the reverse_proxy,
			// or the stream arrives in blocks rather than as it is written.
			'x-accel-buffering': 'no'
		}
	});
};
