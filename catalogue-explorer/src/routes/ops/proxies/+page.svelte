<script lang="ts">
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, stateTone } from '$lib/ops/format';

	let { data, form } = $props();
	const bytes = (value: number | null | undefined) =>
		value == null ? '—' : `${(value / 1_000_000).toLocaleString('en-GB', { maximumFractionDigits: 1 })} MB`;
	const percent = (value: number, maximum: number) => maximum ? Math.min(100, 100 * value / maximum) : 0;
	const admin = $derived(data.operator?.role === 'admin');
	const operator = $derived(data.operator!);
	const overview = $derived(data.overview!);
	const profiles = $derived(data.profiles ?? []);
	const reservations = $derived(data.reservations ?? []);
	const cycle = $derived(data.overview?.cycle);
	const accounted = $derived(cycle?.accounted_bytes ?? 0);
	const discrepancy = $derived((cycle?.provider_reported_bytes ?? 0) - (cycle?.application_bytes ?? 0));
</script>

<svelte:head><title>Proxies · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<div class="mb-5 flex flex-wrap items-start justify-between gap-3">
		<div>
			<h1 class="text-lg font-semibold">Proxy manager</h1>
			<p class="text-base-content/60 text-sm">Decodo Residential · 3 GB provider ceiling · 2.4 GB operational ceiling</p>
		</div>
		<div class="flex gap-2">
			<span class="badge {overview.deployment_enabled ? 'badge-success' : 'badge-error'}">
				deployment {overview.deployment_enabled ? 'enabled' : 'disabled'}
			</span>
			<span class="badge {cycle?.kill_switch ? 'badge-error' : 'badge-success'}">
				{cycle?.kill_switch ? 'new traffic stopped' : 'leases open'}
			</span>
			<span class="badge badge-outline">{operator.role}</span>
		</div>
	</div>

	{#if form?.error}<div class="alert alert-error mb-4 text-sm">{form.error}</div>{/if}
	{#if form?.ok}<div class="alert alert-success mb-4 text-sm">Operation accepted.</div>{/if}

	<section class="grid gap-4 lg:grid-cols-3">
		<div class="card bg-base-100 shadow-sm lg:col-span-2">
			<div class="card-body p-4">
				<div class="flex items-center justify-between gap-3">
					<h2 class="card-title text-sm">Current cycle</h2>
					<span class="text-base-content/60 text-xs">reconciled {relative(cycle?.reconciled_at)}</span>
				</div>
				{#if cycle}
					<div class="mt-2 h-5 overflow-hidden rounded bg-base-200" title={`${bytes(accounted)} accounted`}>
						<div class="h-full bg-primary" style={`width:${percent(accounted, cycle.purchased_bytes)}%`}></div>
					</div>
					<div class="text-base-content/60 mt-1 flex justify-between text-xs">
						<span>{bytes(accounted)} accounted</span>
						<span>{bytes(cycle.operational_bytes)} operational / {bytes(cycle.purchased_bytes)} purchased</span>
					</div>
					<div class="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
						<div><span class="text-base-content/50 block text-xs">Provider</span>{bytes(cycle.provider_reported_bytes)}</div>
						<div><span class="text-base-content/50 block text-xs">Application</span>{bytes(cycle.application_bytes)}</div>
						<div><span class="text-base-content/50 block text-xs">Reserved</span>{bytes(cycle.active_reserved_bytes)}</div>
						<div><span class="text-base-content/50 block text-xs">Headroom</span>{bytes(cycle.remaining_operational_bytes)}</div>
					</div>
					<p class="text-base-content/60 mt-2 text-xs">Today {bytes(cycle.daily_used_bytes)} · dynamic daily allowance {bytes(cycle.dynamic_daily_bytes)}</p>
					<p class="mt-3 text-xs {discrepancy > 10_000_000 ? 'text-warning' : 'text-base-content/60'}">Provider − application discrepancy: {bytes(discrepancy)}. Provider totals remain authoritative for headroom.</p>
				{:else}<p class="text-warning text-sm">No active billing cycle. Leases fail closed.</p>{/if}
			</div>
		</div>

		<div class="card bg-base-100 shadow-sm">
			<div class="card-body gap-2 p-4">
				<h2 class="card-title text-sm">Controls</h2>
				{#if admin}
					<form method="POST" action="?/reconcile"><button class="btn btn-sm w-full">Reconcile now</button></form>
					<form method="POST" action="?/kill">
						<input type="hidden" name="mode" value="activate" />
						<button class="btn btn-error btn-sm w-full">Stop new paid traffic</button>
					</form>
					<form method="POST" action="?/kill"><input type="hidden" name="mode" value="clear" /><input class="input input-xs w-full" name="confirmation" placeholder="ENABLE PAID PROXY TRAFFIC" required /><button class="btn btn-sm mt-1 w-full">Clear stop</button></form>
					<form method="POST" action="?/kill"><input type="hidden" name="mode" value="revoke" /><input class="input input-xs w-full" name="confirmation" placeholder="REVOKE ACTIVE PROXY LEASES" required /><button class="btn btn-warning btn-sm mt-1 w-full">Revoke active leases</button></form>
					<div class="grid grid-cols-2 gap-2">
						<form method="POST" action="?/pilot"><input type="hidden" name="mode" value="start" /><input class="input input-xs w-full" name="confirmation" placeholder="START PAID PROXY PILOT" required /><button class="btn btn-sm mt-1 w-full">Start pilot</button></form>
						<form method="POST" action="?/pilot"><input type="hidden" name="mode" value="stop" /><button class="btn btn-sm w-full">Stop pilot</button></form>
					</div>
				{:else}<p class="text-base-content/60 text-sm">Viewer access is read-only.</p>{/if}
			</div>
		</div>
	</section>

	<section class="mt-5 card bg-base-100 shadow-sm">
		<div class="card-body p-4">
			<div class="flex items-center justify-between">
				<h2 class="card-title text-sm">Billing cycles</h2>
				{#if admin}<form method="POST" action="?/proposeCycle"><button class="btn btn-sm">Propose from Decodo</button></form>{/if}
			</div>
			<div class="overflow-x-auto">
				<table class="table table-sm">
					<thead><tr><th>Status</th><th>UTC boundary</th><th>Purchased</th><th>Operational</th><th></th></tr></thead>
					<tbody>{#each data.cycles as item (item.id)}
						<tr><td><span class="badge {stateTone(item.lifecycle)}">{item.lifecycle}</span></td><td>{item.cycle_start} → {item.cycle_end}</td><td>{bytes(item.purchased_bytes)}</td><td>{bytes(item.operational_bytes)}</td><td>
							{#if admin && (item.lifecycle === 'proposed' || item.lifecycle === 'active')}
								<form method="POST" action="?/cycle"><input type="hidden" name="id" value={item.id} /><input type="hidden" name="mode" value={item.lifecycle === 'proposed' ? 'open' : 'close'} /><input type="hidden" name="cycle" value={JSON.stringify(item)} /><input class="input input-xs" name="confirmation" placeholder={item.lifecycle === 'proposed' ? 'OPEN DECODO CYCLE' : 'CLOSE DECODO CYCLE'} required /><button class="btn btn-xs">{item.lifecycle === 'proposed' ? 'Open confirmed' : 'Close expired'}</button></form>
							{/if}
						</td></tr>
					{/each}</tbody>
				</table>
			</div>
		</div>
	</section>

	<section class="mt-5 grid gap-4 xl:grid-cols-2">
		<div class="card bg-base-100 shadow-sm"><div class="card-body p-4">
			<div class="flex justify-between"><h2 class="card-title text-sm">Profiles / Decodo sub-users</h2>{#if admin}<form method="POST" action="?/refreshProfiles"><button class="btn btn-xs">Refresh</button></form>{/if}</div>
			{#if admin}
				<form method="POST" action="?/createProfile" class="grid grid-cols-2 gap-2 rounded bg-base-200 p-3 text-sm">
					<input class="input input-sm" name="logical_name" placeholder="logical_name" required />
					<input class="input input-sm" name="display_name" placeholder="Display name" required />
					<input class="input input-sm" name="allocated_mb" type="number" min="1" max="2400" value="100" required />
					<input class="input input-sm" name="limit_mb" type="number" min="1" max="2400" value="100" required />
					<input class="input input-sm col-span-2" name="confirmation" placeholder="CREATE logical_name" required />
					<button class="btn btn-primary btn-sm col-span-2">Create bounded sub-user</button>
				</form>
			{/if}
			<div class="divide-base-200 divide-y">{#each data.profiles as profile (profile.id)}
				<div class="py-3 text-sm"><div class="flex items-center justify-between"><strong>{profile.display_name}</strong><span class="badge {profile.enabled ? 'badge-success' : 'badge-ghost'}">{profile.lifecycle}</span></div>
					<p class="text-base-content/60 text-xs">{profile.logical_name} · {profile.username_mask ?? 'not installed'} · allocation {bytes(profile.allocated_bytes)} · limit {bytes(profile.provider_traffic_limit_bytes)} · generation {profile.secret_generation}</p>
					{#if admin}<div class="mt-2 grid gap-2"><form method="POST" action="?/profile" class="flex gap-1"><input type="hidden" name="id" value={profile.id}/><input type="hidden" name="logical_name" value={profile.logical_name}/><input type="hidden" name="mode" value="rotate"/><select class="select select-xs" name="rotation_mode"><option value="drain">Drain first</option><option value="blue-green">Blue-green</option></select><input class="input input-xs" name="confirmation" placeholder={`ROTATE ${profile.logical_name}`} required/><button class="btn btn-xs">Rotate</button></form><form method="POST" action="?/profile" class="flex gap-1"><input type="hidden" name="id" value={profile.id}/><input type="hidden" name="logical_name" value={profile.logical_name}/><input type="hidden" name="mode" value="disable"/><input class="input input-xs" name="confirmation" placeholder={`DISABLE ${profile.logical_name}`} required/><button class="btn btn-xs btn-warning">Disable</button></form><form method="POST" action="?/profile" class="flex gap-1"><input type="hidden" name="id" value={profile.id}/><input type="hidden" name="logical_name" value={profile.logical_name}/><input type="hidden" name="mode" value="retire"/><input class="input input-xs" name="confirmation" placeholder={`RETIRE ${profile.logical_name}`} required/><button class="btn btn-xs btn-error">Retire</button></form></div>{/if}
				</div>
			{/each}</div>
		</div></div>

		<div class="card bg-base-100 shadow-sm"><div class="card-body p-4">
			<h2 class="card-title text-sm">Routes and paid probe</h2>
			<p class="text-base-content/60 text-xs">Saving a route spends nothing. A probe streams at most 1 MB of application data and reserves a 1.1 MB provider envelope. A new session does not guarantee a unique exit.</p>
			{#if admin && profiles.length}
				<form method="POST" action="?/createRoute" class="grid grid-cols-2 gap-2 rounded bg-base-200 p-3 text-sm">
					<input class="input input-sm" name="label" placeholder="Route label" required />
					<select class="select select-sm" name="profile_id">{#each profiles.filter((p: any) => p.enabled) as profile}<option value={profile.id}>{profile.logical_name}</option>{/each}</select>
					<input class="input input-sm" name="country" maxlength="2" placeholder="Country, e.g. FR" />
					<select class="select select-sm" name="protocol"><option>http</option><option>https</option><option>socks5</option></select>
					<select class="select select-sm" name="session_mode"><option>random</option><option>sticky</option></select>
					<input class="input input-sm" name="session_minutes" type="number" min="1" max="1440" value="30" />
					<input class="input input-sm" name="max_mb" type="number" min="2" max="25" value="25" />
					<label class="label justify-start gap-2"><input class="checkbox checkbox-sm" name="enabled" type="checkbox" /> enabled</label>
					<button class="btn btn-primary btn-sm col-span-2">Save route — no traffic</button>
				</form>
			{/if}
			<div class="divide-base-200 divide-y">{#each data.routes as route (route.id)}<div class="flex items-center justify-between gap-2 py-3 text-sm"><div><strong>{route.label}</strong><p class="text-base-content/60 text-xs">{route.profile} · {route.protocol} · {route.country ?? 'any'} · {route.session_mode} · {bytes(route.max_bytes)}</p></div>{#if admin}<div class="grid gap-1"><form method="POST" action="?/route" class="flex gap-1"><input type="hidden" name="id" value={route.id}/><input type="hidden" name="mode" value="probe"/><input class="input input-xs" name="confirmation" placeholder="SPEND UP TO 1.1 MB" required/><button class="btn btn-xs btn-primary" disabled={!overview.paid_probe_enabled}>Test session</button></form><form method="POST" action="?/route"><input type="hidden" name="id" value={route.id}/><input type="hidden" name="mode" value="delete"/><button class="btn btn-xs">Retire route</button></form></div>{/if}</div>{/each}</div>
		</div></div>
	</section>

	<section class="mt-5 grid gap-4 xl:grid-cols-2">
		<div class="card bg-base-100 shadow-sm"><div class="card-body p-4"><h2 class="card-title text-sm">Reservations</h2><div class="overflow-x-auto"><table class="table table-xs"><thead><tr><th>Consumer</th><th>Profile</th><th>Reserved / used</th><th>State</th></tr></thead><tbody>{#each reservations.slice(0, 50) as row (row.id)}<tr><td>{row.source_id ?? `probe ${row.probe_id?.slice(0, 8)}`}</td><td>{row.profile}</td><td>{bytes(row.reserved_bytes)} / {bytes(row.estimated_bytes)}</td><td><span class="badge badge-xs {stateTone(row.state)}">{row.state}</span></td></tr>{/each}</tbody></table></div></div></div>
		<div class="card bg-base-100 shadow-sm"><div class="card-body p-4"><h2 class="card-title text-sm">Recent probes</h2><div class="overflow-x-auto"><table class="table table-xs"><thead><tr><th>When</th><th>Exit</th><th>Traffic</th><th>Result</th></tr></thead><tbody>{#each data.probes as row (row.id)}<tr><td>{relative(row.requested_at)}</td><td>{row.exit_country ?? '—'} {row.exit_ip ?? ''}</td><td>{bytes(row.estimated_bytes)}</td><td>{row.state}</td></tr>{/each}</tbody></table></div></div></div>
	</section>

	<section class="mt-5 card bg-base-100 shadow-sm"><div class="card-body p-4"><h2 class="card-title text-sm">Usage by day</h2><div class="overflow-x-auto"><table class="table table-xs"><thead><tr><th>Day</th><th>Download</th><th>Upload</th><th>Total</th><th>Requests</th></tr></thead><tbody>{#each data.usage as row}<tr><td>{String(row.key)}</td><td>{bytes(row.received_bytes)}</td><td>{bytes(row.transmitted_bytes)}</td><td>{bytes(row.total_bytes)}</td><td>{(row.request_count ?? 0).toLocaleString()}</td></tr>{/each}</tbody></table></div></div></section>

	<section class="mt-5 card bg-base-100 shadow-sm"><div class="card-body p-4"><h2 class="card-title text-sm">Source policy candidates</h2><p class="text-base-content/60 text-xs">Only sources explicitly marked proxy-eligible appear here. “Always” remains blocked until three successful proxy evidence runs promote the source.</p><div class="overflow-x-auto"><table class="table table-xs"><thead><tr><th>Source</th><th>Failures / runs</th><th>Evidence</th><th>Policy</th></tr></thead><tbody>{#each data.candidates as row (row.source_id)}<tr><td>{row.source_id}</td><td>{row.failures} / {row.runs}</td><td>{row.evidence_state ?? 'unproven'} ({row.evidence_count ?? 0})</td><td>{#if admin}<form method="POST" action="?/sourcePolicy" class="flex gap-1"><input type="hidden" name="source_id" value={row.source_id}/><select class="select select-xs" name="policy"><option value="never" selected={row.policy === 'never'}>never</option><option value="fallback" selected={row.policy === 'fallback'}>fallback</option><option value="always" selected={row.policy === 'always'}>always</option></select><select class="select select-xs" name="route_id"><option value="">no route</option>{#each data.routes as route}<option value={route.id} selected={row.route_id === route.id}>{route.label}</option>{/each}</select><input class="input input-xs w-20" name="max_megabytes" type="number" min="1" max="25" value="25"/><button class="btn btn-xs">Apply</button></form>{:else}{row.policy ?? 'never'}{/if}</td></tr>{/each}</tbody></table></div></div></section>

	<section class="mt-5 card bg-base-100 shadow-sm"><div class="card-body p-4"><h2 class="card-title text-sm">Audit</h2><div class="overflow-x-auto"><table class="table table-xs"><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Resource</th><th>Result</th></tr></thead><tbody>{#each data.audit as row (row.id)}<tr><td>{relative(row.at)}</td><td>{row.actor}</td><td>{row.action}</td><td>{row.resource_type} {row.resource_id ?? ''}</td><td>{row.state}{row.error_code ? ` · ${row.error_code}` : ''}</td></tr>{/each}</tbody></table></div></div></section>
{/if}
