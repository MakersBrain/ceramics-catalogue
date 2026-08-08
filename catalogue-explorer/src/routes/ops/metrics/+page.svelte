<script lang="ts">
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import Sparkline from '$lib/ops/Sparkline.svelte';
	import { count } from '$lib/ops/format';

	let { data } = $props();

	const totalStale = $derived(
		(data.staleness ?? []).reduce((sum: number, band: any) => sum + band.sources, 0)
	);
</script>

<svelte:head><title>Metrics · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<h1 class="mb-4 text-lg font-semibold">Metrics</h1>

	<div class="grid gap-4 lg:grid-cols-2">
		<div class="card bg-base-100 shadow-sm">
			<div class="card-body p-4">
				<h2 class="card-title text-sm">Records collected per day</h2>
				<Sparkline
					points={(data.runs ?? []).map((r: any) => ({ label: r.day, value: r.records }))}
					format={count}
				/>
			</div>
		</div>

		<div class="card bg-base-100 shadow-sm">
			<div class="card-body p-4">
				<h2 class="card-title text-sm">Median run duration</h2>
				<Sparkline
					points={(data.runs ?? []).map((r: any) => ({
						label: r.day,
						value: Math.round(r.median_seconds ?? 0)
					}))}
					format={(v) => `${Math.round(v / 60)}m`}
				/>
			</div>
		</div>

		<div class="card bg-base-100 shadow-sm">
			<div class="card-body p-4">
				<h2 class="card-title text-sm">Price observations written per day</h2>
				<Sparkline
					points={(data.offers ?? []).map((o: any) => ({ label: o.day, value: o.offers }))}
					format={count}
				/>
			</div>
		</div>

		<div class="card bg-base-100 shadow-sm">
			<div class="card-body p-4">
				<h2 class="card-title text-sm">Source freshness</h2>
				<p class="text-base-content/60 mb-2 text-xs">
					A source that has silently stopped returning records is the failure this whole
					pipeline exists to catch.
				</p>
				<div class="flex h-6 overflow-hidden rounded">
					{#each data.staleness ?? [] as band (band.band)}
						{@const share = totalStale ? (100 * band.sources) / totalStale : 0}
						<div
							class="flex items-center justify-center text-xs
							{band.band === 'fresh' ? 'bg-success/70' : band.band === 'ageing' ? 'bg-warning/70' : 'bg-error/70'}"
							style="width: {share}%"
							title="{band.band}: {band.sources} sources"
						>
							{band.sources}
						</div>
					{/each}
				</div>
				<div class="text-base-content/60 mt-2 flex gap-4 text-xs">
					<span>fresh &lt; 36h</span><span>ageing &lt; 7d</span><span>stale</span>
				</div>
			</div>
		</div>

		<div class="card bg-base-100 shadow-sm lg:col-span-2">
			<div class="card-body p-4">
				<h2 class="card-title text-sm">Errors by host, last 7 days</h2>
				{#if (data.hosts ?? []).length === 0}
					<p class="text-base-content/60 text-sm">No errors recorded.</p>
				{:else}
					{@const worst = Math.max(1, ...(data.hosts ?? []).map((h: any) => h.errors))}
					<div class="grid gap-1">
						{#each data.hosts ?? [] as host (host.host)}
							<div class="flex items-center gap-2 text-xs">
								<span class="w-56 truncate" title={host.host}>{host.host}</span>
								<div class="bg-base-200 h-3 flex-1 overflow-hidden rounded">
									<div class="bg-error/70 h-full" style="width: {(100 * host.errors) / worst}%"></div>
								</div>
								<span class="w-16 text-right tabular-nums">{count(host.errors)}</span>
								<span class="text-base-content/40 w-16 text-right">{host.jobs} jobs</span>
							</div>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	</div>

	<p class="text-base-content/50 mt-6 text-xs">
		These read PostgreSQL directly. The same quantities are exposed in Prometheus format at
		<code>/metrics</code> on the worker and the control service, for when something scrapes them.
	</p>
{/if}
