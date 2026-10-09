# Appending to v0.3 stores

`chronozarr append STORE INPUT` adds strictly later timesteps on the existing grid. Unsharded stores write new chunks and updated metadata. Sharded stores rewrite the trailing shard; choose finite `--shard-time` when object count justifies that cost. Existing values and coordinates stay fixed. Publish data before metadata and root metadata last; invalidate cached mutable objects after publication.

Measured costs are in [evidence.md](evidence.md#appending): an unsharded append wrote about 55 MB per month, and a twelve-month shard cycle wrote 4,389 MB against 686 MB unsharded.
