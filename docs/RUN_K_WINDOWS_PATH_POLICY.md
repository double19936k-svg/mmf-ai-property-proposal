# Run K Windows path policy

Deep isolated data directories on Windows with LongPathsEnabled=0 must be passed
through the extended Win32 namespace (absolute local paths prefixed by `\\?\`).
The offline regression harness applies this spelling to its redirected data and
temporary roots, and records the physical directory separately. Test membership,
assertions, output location, and environment-wide registry settings stay intact.
This is a run-local environment policy, not proof of arbitrary unadapted deep
paths or a fresh-machine installation.

Atomic JSON writers adapt long paths at the filesystem boundary. Failures still
raise, previous evidence survives, and temporary files are cleaned. The renderer
reads a copy of the final DOCX under a fixed short basename; the original DOCX is
never converted in place. A deep local render directory receives a temporary
drive alias pointing to that same physical directory. Each render has a unique
profile URI, including when an alias is reused. Node module resolution removes
the Win32 namespace prefix before resolving and importing the same physical files.
Allocation is serialized,
occupied drives are skipped, and exact-target cleanup runs on success/failure.
If no alias is available, pagination reports RENDER_FAILED; it cannot claim
measured page numbers. UNC renderer roots are not certified.

Release evidence reads the candidate's exact requirements and Node declarations,
checks the bundled Node package and build version, and hashes its metadata sources.
It does not certify Python installation, passed regression, Office acceptance,
Provider generation, or release readiness.

Tracked text uses LF through .gitattributes. Source hashes and package hashes must
be recomputed and frozen after any physical-byte change, including normalization.
