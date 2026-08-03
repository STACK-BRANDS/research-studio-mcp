"""Site-capture connectors for the Research Studio worker (Phase 2 of the
deep-research plan). Each connector implements a `(job, claimant, plan) ->
dict` entrypoint dispatched by `worker.jobs._dispatch` for a specific
`(job_kind='scrape', connector=<name>)` pair. `worker.connectors.base` holds
the discipline every connector shares (robots.txt compliance, per-domain
rate limiting); `worker.connectors.web_fetch` is the first (and, as of
Phase 2, only) connector.
"""
