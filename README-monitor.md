# Supplier Price Monitor — next validation

The new `price_monitor.py` creates `output/Supplier_Prices.xlsx` with Current, Changes, History and Errors tabs, and `data/price_history.csv`. It currently runs **manually** through the Price report workflow.

**Important:** the price selector is provisional. The first visible matching price may belong to a recommendation or promotional widget. Validate the actual three prices against screenshots before enabling daily automation or treating them as reliable data.

The workflow uploads the XLSX and CSV as a downloadable artifact. GitHub Actions runners are ephemeral: artifacts do not provide durable price history across runs. We must add persistent storage and Drive delivery after validation.

Google Drive automated upload is not configured. It requires separate, narrowly scoped credentials stored as GitHub Actions secrets; do not commit credentials to the repository.
