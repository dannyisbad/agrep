"""hookless - observe any agent on the box without touching its config.

The observation layer: process discovery (procscan), live session state (live),
agent launching with ground-truth capture
(capture), and per-agent resolution glue (native). Everything here derives
state from what agents already emit - files, processes, stores - never from
callbacks wired into their settings.

THE LAW (enforced by selftest): this package imports nothing from the product
layer (corpusdb/search/explore/...). Product code consumes hookless,
never the reverse. The boundary is what lets this split into its own package
once the interface has stopped moving - agrep is the first subscriber, not
the owner.

Import submodules directly (`from hookless import live`); __init__ stays empty
so consumers that only need one module don't pay the others' imports.
"""
