<script lang="ts">
	// The control service is optional from the explorer's point of view: /explore
	// and /compare read the database directly and work without it. So this is a
	// clear explanation rather than an error page.
	let { reason }: { reason: string } = $props();
</script>

<div class="alert alert-warning">
	<div>
		<h2 class="font-semibold">The control service is not reachable</h2>
		<p class="mt-1 text-sm">{reason}</p>
		<p class="mt-2 text-sm">
			Set <code>CATALOGUE_CONTROL_URL</code> and <code>CATALOGUE_CONTROL_TOKEN</code> for the
			explorer, and start the service with
			<code>docker compose up control</code>. The catalogue pages work without it.
		</p>
	</div>
</div>
