<script lang="ts">
	// The control service is optional from the explorer's point of view: /explore
	// and /compare read the database directly and work without it. So this is a
	// clear explanation rather than an error page.
	import { Notice } from '$lib/components/ui/notice';

	let { reason }: { reason: string } = $props();
	const operatorProblem = $derived(reason.startsWith('Operator identity'));
</script>

<Notice kind="warning">
	<div>
		<h2 class="font-sans text-sm font-semibold">
			{operatorProblem ? 'Operator access is required' : 'The control service is not reachable'}
		</h2>
		<p class="mt-1 text-sm">{reason}</p>
		{#if operatorProblem}
			<p class="mt-2 text-sm">
				Configure the trusted identity header at the access proxy and add the exact identity to
				<code>CATALOGUE_OPERATOR_VIEWERS</code> or <code>CATALOGUE_OPERATOR_ADMINS</code>, then
				redeploy the explorer.
			</p>
		{:else}
			<p class="mt-2 text-sm">
				Set <code>CATALOGUE_CONTROL_URL</code> and <code>CATALOGUE_CONTROL_TOKEN</code> for the
				explorer, and start the service with
				<code>docker compose up control</code>. The catalogue pages work without it.
			</p>
		{/if}
	</div>
</Notice>
