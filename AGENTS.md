# Papertrail

This directory is an independent politician disclosure app, separate from the parent RUN/WAY spend tracker. Keep changes scoped here.

User decisions: Quiver-style searchable politician trades and reported holdings; select individual politicians for email alerts; use frontend-design and Taste guidance for a clean table-first dashboard; deploy to Vercel. Preserve the selected politician across sign-in. No paid Quiver features or automatic brokerage trades requested.

Use the Python CLI as the data implementation for hosted searches. Clearly identify reported ranges, annual snapshot dates, uncertain OCR and incomplete coverage. Never present reconstructed holdings as a live portfolio. Keep watchlists private and credentials/browser profiles out of deployment. Do not subscribe to politicians without the user's selection.

2026-10-07 design feedback: the user rejected the initial blue sidebar dashboard and supplied a Revolut design-system reference. Use true-black and white full-width bands, generous tightly tracked sans-serif display type, Inter body, pill controls, flat surfaces and scarce cobalt-violet. Keep data and politician search functional and prominent. The reference takes precedence over generic Taste styling.

Brand: Papertrail, chosen after the user requested a more interesting name; generated black P/document mark in public/papertrail-mark.png. Preserve the original generated asset.

Copy-trading objective: guide the user through disclosed purchases, source checks, reporting delays, buys versus sales and manual broker decisions. Disclosures cannot establish current positions or exact execution prices. Reviewed OGE rows must match the exact source URL and PDF digest; label the review as AI, not human or independent audit.
