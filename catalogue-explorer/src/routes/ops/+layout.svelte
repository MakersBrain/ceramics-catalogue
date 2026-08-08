<script lang="ts">
	import { page } from '$app/state';
	import { invalidateAll } from '$app/navigation';
	import { onMount, setContext } from 'svelte';
	import { OpsStream } from '$lib/ops/stream.svelte';
	import ConnectionBadge from '$lib/ops/ConnectionBadge.svelte';

	let { children } = $props();

	// One subscription for the whole section. Every page reads from this store;
	// only /ops/runs/[id] opens a second, narrower one, so a browser never has
	// more than two streams open against the six-per-origin cap.
	const stream = new OpsStream('workers,notifications,runs');
	setContext('ops-stream', stream);

	onMount(() => {
		stream.connect(() => invalidateAll());
		return () => stream.disconnect();
	});

	const tabs = [
		{ href: '/ops', label: 'Overview' },
		{ href: '/ops/runs', label: 'Runs' },
		{ href: '/ops/sources', label: 'Sources' },
		{ href: '/ops/notifications', label: 'Notifications' },
		{ href: '/ops/metrics', label: 'Metrics' }
	];

	const current = $derived(page.url.pathname);
	const unacknowledged = $derived(stream.unacknowledged.length);
	const busy = $derived(stream.workers.filter((w) => w.status === 'busy').length);
</script>

<div class="min-h-screen bg-base-200/40">
	<header class="border-base-300 bg-base-100 border-b">
		<div class="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-3">
			<a href="/" class="text-base-content/60 hover:text-base-content text-sm">catalogue</a>
			<span class="text-base-content/30">/</span>
			<span class="font-semibold">operations</span>

			<nav class="ml-4 flex flex-wrap gap-1">
				{#each tabs as tab (tab.href)}
					<a
						href={tab.href}
						class="rounded px-3 py-1 text-sm transition-colors
						{current === tab.href || (tab.href !== '/ops' && current.startsWith(tab.href))
							? 'bg-primary text-primary-content'
							: 'hover:bg-base-200'}"
					>
						{tab.label}
						{#if tab.href === '/ops/notifications' && unacknowledged > 0}
							<span class="badge badge-warning badge-sm ml-1">{unacknowledged}</span>
						{/if}
					</a>
				{/each}
			</nav>

			<div class="ml-auto flex items-center gap-3 text-sm">
				<span class="text-base-content/60">
					{busy}/{stream.workers.length} workers busy
				</span>
				<ConnectionBadge state={stream.connection} />
			</div>
		</div>
	</header>

	<main class="mx-auto max-w-7xl px-4 py-6">
		{@render children()}
	</main>
</div>
