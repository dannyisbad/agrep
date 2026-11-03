# Content-bound handles

A current result handle is `@session:turn.digest`; a tool pointer may add an
event selector and a bounded span. Session prefixes widen to disambiguate, and
identifiers that need encoding use the public opaque-session form.

The four-hex digest checks accidental content drift. It is not cryptographic
integrity. It binds the reference to indexed text so turn renumbering or a
prefix collision cannot silently serve a different message.

| Resolution | Behavior |
|---|---|
| Address and digest match | Serve the named content. |
| Exactly one nearby row matches the claim | Serve it and disclose the moved turn. |
| No matching row or multiple matching rows | Refuse with exit 2. |
| Tool selector or span no longer verifies | Refuse rather than quoting unrelated output. |

Exploratory `around SESSION TURN` can clamp to an existing turn with disclosure.
Exact handles cannot silently clamp. Digestless `@session:turn` forms are rejected as malformed; use `around SESSION TURN` for positional navigation. `around @session` opens the chat's latest
turn; it is a mutable session reference, not a content citation.

Copy a handle from current output and open it before citing it. 0.5 does not
migrate earlier agrep's serialized state or references. Output framing is in
[OUTPUT_CONTRACTS.md](OUTPUT_CONTRACTS.md).
