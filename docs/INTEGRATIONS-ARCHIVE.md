# Integrations and archive

Setup records typed enrollment for marked instruction blocks, exact hook entries,
and owned extension files. Removal and doctor repair act on that enrollment.
Doctor never changes consent choices or archive policy. Archive retention is off
by default; setup without an explicit archive choice preserves the existing setting.

Instruction writes preserve surrounding bytes and follow existing dotfile links.
Restore destinations retain strict no-follow and traversal checks. Explicit archive
operations wait for the archive lock; scheduled capture yields silently. Empty
source captures never replace the latest nonempty version. Existing content-addressed
blobs are reused; restore verifies their hashes.

Model provisioning uses the semantic module's pinned, verified downloader. The
background task list takes work.lock once, drains embedding batches, reclaims old
unreferenced vector files, captures due archives, and invokes a configured post-index
hook for a changed index. The engine integration adds incremental source refresh at
the front and schedules it from the stat-only census.

Regression fixtures use explicit temporary homes, model paths, and data roots.
The integration suite covers native archive roots, opt-outs, enrolled drift, file
links, restore safety, contention, content hashes, and typed setting preservation.
