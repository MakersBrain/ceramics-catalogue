import type { Actions, PageServerLoad } from './$types';
import { fail } from '@sveltejs/kit';
import { ControlError, configured, del, get, post, put } from '$lib/server/control';
import { operatorFromRequest, operatorProblem, requireSameOrigin } from '$lib/server/operator';
import type {
	ProxyAuditList,
	ProxyCandidateList,
	ProxyCycleList,
	ProxyMutationResult,
	ProxyOverview,
	ProxyProbeList,
	ProxyProfileList,
	ProxyReservationList,
	ProxyRouteList,
	ProxyUsageList
} from '$lib/ops/types';

const message = (error: unknown) => (error instanceof ControlError ? error.message : String(error));

export const load: PageServerLoad = async ({ request }) => {
	if (!configured()) return { unavailable: 'CATALOGUE_CONTROL_TOKEN is not set for the explorer' };
	const operator = operatorFromRequest(request);
	if (!operator) return { unavailable: operatorProblem(request) };
	try {
		const [overview, cycles, usage, reservations, profiles, routes, probes, audit, candidates] =
			await Promise.all([
				get<ProxyOverview>('/v1/proxy/overview', operator),
				get<ProxyCycleList>('/v1/proxy/cycles', operator),
				get<ProxyUsageList>('/v1/proxy/usage?group_by=day', operator),
				get<ProxyReservationList>('/v1/proxy/reservations', operator),
				get<ProxyProfileList>('/v1/proxy/profiles', operator),
				get<ProxyRouteList>('/v1/proxy/routes', operator),
				get<ProxyProbeList>('/v1/proxy/probes', operator),
				get<ProxyAuditList>('/v1/proxy/audit', operator),
				get<ProxyCandidateList>('/v1/proxy/candidates', operator)
			]);
		return {
			operator,
			overview,
			cycles: cycles.cycles,
			usage: usage.usage,
			reservations: reservations.reservations,
			profiles: profiles.profiles,
			routes: routes.routes,
			probes: probes.probes,
			audit: audit.audit,
			candidates: candidates.candidates
		};
	} catch (error) {
		return { unavailable: message(error), operator };
	}
};

const authorized = (request: Request, url: URL) => {
	requireSameOrigin(request, url);
	const operator = operatorFromRequest(request);
	if (!operator || operator.role !== 'admin') throw new Error('administrator role is required');
	return operator;
};

const result = async (work: () => Promise<unknown>) => {
	try {
		return { ok: true, result: await work() };
	} catch (error) {
		return fail(error instanceof ControlError ? error.status : 400, { error: message(error) });
	}
};

export const actions: Actions = {
	reconcile: async ({ request, url }) => {
		const operator = authorized(request, url);
		return result(() => post<ProxyMutationResult>('/v1/proxy/reconcile', {}, operator));
	},
	kill: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		const action = String(form.get('mode'));
		if (!['activate', 'clear', 'revoke'].includes(action)) return fail(422, { error: 'invalid kill-switch action' });
		const confirmation = String(form.get('confirmation') || '') || undefined;
		return result(() => post(`/v1/proxy/kill-switch/${action}`, { confirmation }, operator));
	},
	pilot: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		const action = String(form.get('mode'));
		if (!['start', 'stop'].includes(action)) return fail(422, { error: 'invalid pilot action' });
		return result(() => post(`/v1/proxy/pilot/${action}`, {
			confirmation: String(form.get('confirmation') || '') || undefined
		}, operator));
	},
	proposeCycle: async ({ request, url }) => {
		const operator = authorized(request, url);
		return result(() => post('/v1/proxy/cycles/propose', {}, operator));
	},
	cycle: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		const id = String(form.get('id'));
		const action = String(form.get('mode'));
		const cycle = JSON.parse(String(form.get('cycle')));
		return result(() =>
			post(`/v1/proxy/cycles/${id}/${action}`, {
				...cycle,
				confirmation: String(form.get('confirmation') || '')
			}, operator)
		);
	},
	createProfile: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		return result(() =>
			post('/v1/proxy/profiles', {
				logical_name: String(form.get('logical_name')),
				display_name: String(form.get('display_name')),
				allocated_bytes: Number(form.get('allocated_mb')) * 1_000_000,
				provider_traffic_limit_bytes: Number(form.get('limit_mb')) * 1_000_000,
				confirmation: String(form.get('confirmation') || '')
			}, operator)
		);
	},
	refreshProfiles: async ({ request, url }) => {
		const operator = authorized(request, url);
		return result(() => post('/v1/proxy/profiles/refresh', {}, operator));
	},
	profile: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		const id = String(form.get('id'));
		const action = String(form.get('mode'));
		const body = action === 'rotate'
			? { mode: String(form.get('rotation_mode') || 'drain'), confirmation: String(form.get('confirmation') || '') }
			: action === 'limit'
				? { provider_traffic_limit_bytes: Number(form.get('bytes')) }
				: action === 'allocation'
					? { allocated_bytes: Number(form.get('bytes')) }
					: action === 'disable'
						? { confirmation: String(form.get('confirmation') || '') }
						: {};
		return result(() =>
			action === 'retire'
				? del(`/v1/proxy/profiles/${id}`, operator, { confirmation: String(form.get('confirmation') || '') })
				: action === 'limit' || action === 'allocation'
					? put(`/v1/proxy/profiles/${id}/${action}`, body, operator)
					: post(`/v1/proxy/profiles/${id}/${action}`, body, operator)
		);
	},
	createRoute: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		return result(() => post('/v1/proxy/routes', {
			label: String(form.get('label')),
			profile_id: String(form.get('profile_id')),
			protocol: String(form.get('protocol') || 'http'),
			country: String(form.get('country') || '') || null,
			session_mode: String(form.get('session_mode') || 'random'),
			session_minutes: Number(form.get('session_minutes') || 30),
			max_bytes: Number(form.get('max_mb') || 25) * 1_000_000,
			pilot: true,
			enabled: form.get('enabled') === 'on'
		}, operator));
	},
	route: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		const id = String(form.get('id'));
		const action = String(form.get('mode'));
		if (action === 'delete') return result(() => del(`/v1/proxy/routes/${id}`, operator));
		if (action === 'probe') return result(() => post(`/v1/proxy/routes/${id}/probe`, { confirmation: String(form.get('confirmation') || '') }, operator));
		return fail(422, { error: 'invalid route action' });
	},
	sourcePolicy: async ({ request, url }) => {
		const operator = authorized(request, url);
		const form = await request.formData();
		const id = String(form.get('source_id'));
		return result(() => put(`/v1/sources/${id}`, {
			proxy: {
				policy: String(form.get('policy')),
				route_id: String(form.get('route_id') || '') || null,
				max_megabytes: Number(form.get('max_megabytes') || 25),
				pilot: true
			}
		}, operator));
	}
};
