<script lang="ts" module>
	import { cn, type WithElementRef } from '$lib/utils';
	import type { HTMLAnchorAttributes, HTMLButtonAttributes } from 'svelte/elements';
	import { type VariantProps, tv } from 'tailwind-variants';

	/**
	 * The same variant/size API `packages/ui` exposes, dressed in the brand.
	 *
	 * Two deliberate departures from the sera defaults that set ships with, both
	 * because the brand legislates the property and sera's answer contradicts it:
	 *
	 *  - Rounded, not `rounded-none`. `--radius` comes from `--mb-radius-md`, and
	 *    the brand calls its scale "deliberately modest" rather than absent.
	 *  - No `uppercase tracking-widest`. The brand reserves that treatment for
	 *    micro labels - eyebrows and table headers - and a page where the column
	 *    headings and the buttons share it has lost the distinction.
	 *
	 * `xs` and `sm` are not conveniences here. The ops pages put twenty-five
	 * controls in one toolbar, and the default 2.5rem control would push the
	 * table the operator came for below the fold.
	 */
	export const buttonVariants = tv({
		base: "focus-visible:ring-ring/50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 group/button inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-md border border-transparent bg-clip-padding font-semibold whitespace-nowrap transition-[background-color,border-color,color] outline-none select-none focus-visible:ring-2 active:translate-y-px disabled:pointer-events-none disabled:opacity-45 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
		variants: {
			variant: {
				/* One filled button per view: if two things look equally primary,
				   neither is. Filled is clay-600 and not the clay-500 brand tone,
				   because white on 500 lands at 4.25:1 and fails AA. */
				default: 'bg-primary text-primary-foreground hover:bg-primary/90',
				secondary: 'border-input bg-card text-foreground hover:bg-secondary',
				outline: 'border-input hover:bg-accent hover:text-accent-foreground bg-transparent',
				ghost: 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
				/* Tinted rather than filled. A destructive control that is a solid
				   red slab reads as the primary action of the view, which is the
				   opposite of what it should invite. */
				destructive:
					'text-destructive bg-destructive/10 hover:bg-destructive/20 focus-visible:ring-destructive/30',
				/* The middle rung. The proxy page has a real ladder - stop new
				   traffic, revoke live leases, retire a sub-user - and collapsing
				   the first two into `destructive` would make the reversible one
				   look exactly like the one that is not. Violet, from the
				   validated trio, so it is not a second red. */
				warning: 'text-warning bg-warning/10 hover:bg-warning/20 focus-visible:ring-warning/30',
				link: 'text-accent-foreground underline-offset-4 hover:underline'
			},
			size: {
				default: 'h-10 px-4 text-sm',
				lg: 'h-11 px-6 text-sm',
				sm: 'h-8 gap-1 px-3 text-xs',
				xs: "h-7 gap-1 px-2 text-xs [&_svg:not([class*='size-'])]:size-3",
				icon: 'size-10',
				'icon-sm': "size-8 [&_svg:not([class*='size-'])]:size-3.5",
				'icon-xs': "size-7 [&_svg:not([class*='size-'])]:size-3"
			}
		},
		defaultVariants: {
			variant: 'default',
			size: 'default'
		}
	});

	export type ButtonVariant = VariantProps<typeof buttonVariants>['variant'];
	export type ButtonSize = VariantProps<typeof buttonVariants>['size'];

	export type ButtonProps = WithElementRef<HTMLButtonAttributes> &
		WithElementRef<HTMLAnchorAttributes> & {
			variant?: ButtonVariant;
			size?: ButtonSize;
		};
</script>

<script lang="ts">
	let {
		class: className,
		variant = 'default',
		size = 'default',
		ref = $bindable(null),
		href = undefined,
		type = 'button',
		disabled,
		children,
		...restProps
	}: ButtonProps = $props();
</script>

{#if href}
	<a
		bind:this={ref}
		data-slot="button"
		class={cn(buttonVariants({ variant, size }), className)}
		href={disabled ? undefined : href}
		aria-disabled={disabled}
		role={disabled ? 'link' : undefined}
		tabindex={disabled ? -1 : undefined}
		{...restProps}
	>
		{@render children?.()}
	</a>
{:else}
	<button
		bind:this={ref}
		data-slot="button"
		class={cn(buttonVariants({ variant, size }), className)}
		{type}
		{disabled}
		{...restProps}
	>
		{@render children?.()}
	</button>
{/if}
