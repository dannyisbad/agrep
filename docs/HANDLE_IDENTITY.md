# Content-bound handles

A current result handle is `@session:turn.digest`; a tool pointer may add an
event selector and a bounded span. Session prefixes widen to disambiguate, and
identifiers that need encoding use the public opaque-session form.

A tool pointer's span counts characters of the event's indexed text and marks where
the query matched, as 0.4.9 placed it: the first separator-flexible phrase match
(query words joined by any run of non-alphanumeric characters); for a one-word query,
its best-aligned occurrence; for all-terms hits, the earliest of each term's
best-aligned occurrence; for content-terms hits, the earliest occurrence of any
content term. Regex and whole-word searches use their first match.

A tool pointer's event selector hashes the session, the call's time and its indexed
text. Parallel calls of one turn can share all three; each of those after the first by
call id hashes its call id too, so that a selector names one call.

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
