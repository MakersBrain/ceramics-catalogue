import { env } from '$env/dynamic/private';

/**
 * Server-side access to catalogue-control.
 *
 * The browser never sees the control token and never reaches the control
 * service directly. Every call goes through a SvelteKit server route, which
 * attaches the bearer token here — including the SSE stream, which is why
 * `EventSource` not being able to set headers costs nothing.
 *
 * Not published on the host is defence in depth; this is the authentication
 * boundary.
 */
const DEFAULT_URL = 'http://127.0.0.1:8687';

const base = () => env.CATALOGUE_CONTROL_URL || DEFAULT_URL;
const token = () => env.CATALOGUE_CONTROL_TOKEN || '';

export class ControlError extends Error {
	constructor(
		readonly status: number,
		readonly title: string,
		readonly detail?: string
	) {
		super(detail ? `${title}: ${detail}` : title);
	}
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
	const response = await fetch(`${base()}${path}`, {
		...init,
		headers: {
			...(init.headers ?? {}),
			authorization: `Bearer ${token()}`,
			...(init.body ? { 'content-type': 'application/json' } : {})
		}
	});
	if (!response.ok) {
		// The control service speaks RFC 9457, so the useful message is in the
		// body rather than in the status line.
		let title = response.statusText;
		let detail: string | undefined;
		try {
			const problem = await response.json();
			title = problem.title ?? title;
			detail = problem.detail;
		} catch {
			// A proxy error page rather than the service; the status is all there is.
		}
		throw new ControlError(response.status, title, detail);
	}
	return response;
}

export async function get<T>(path: string): Promise<T> {
	return (await request(path)).json() as Promise<T>;
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
	const response = await request(path, {
		method: 'POST',
		body: JSON.stringify(body ?? {})
	});
	return response.json() as Promise<T>;
}

export async function put<T>(path: string, body: unknown): Promise<T> {
	const response = await request(path, { method: 'PUT', body: JSON.stringify(body) });
	return response.json() as Promise<T>;
}

/**
 * Open the upstream event stream with the token attached.
 *
 * Returned as a raw `Response` so the route can hand the body straight back to
 * the browser without buffering it — reading it here would defeat the entire
 * point of a stream.
 */
export async function stream(query: string, lastEventId: string | null): Promise<Response> {
	return fetch(`${base()}/v1/events${query}`, {
		headers: {
			authorization: `Bearer ${token()}`,
			accept: 'text/event-stream',
			...(lastEventId ? { 'last-event-id': lastEventId } : {})
		}
	});
}

export function configured(): boolean {
	return Boolean(env.CATALOGUE_CONTROL_TOKEN);
}
