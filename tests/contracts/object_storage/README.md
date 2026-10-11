# Original-object storage conformance

`test_conformance.py` is the shared observable suite for every
`OriginalObjectStore` implementation. An adapter joins the suite by adding its
factory to the parameterized `original_object_store_factory` fixture; scenarios
must not be copied into adapter-specific tests. Tests use only the storage port,
its typed contracts, and the public one-shot lease boundary.

Each adapter factory also supplies test-only bounded-I/O observations. They let
the shared scenarios reject implementations that return a bounded result only
after reading or buffering beyond the declared limit. A filesystem factory may
collect this evidence by instrumenting its test reader; the scenarios remain
adapter-neutral.

The included `DeterministicOriginalObjectStoreFake` is an async policy/contract
test double. It uses injected time and generation identities and performs no
filesystem, network, or database I/O. Passing the fake suite does **not** prove:

- filesystem durability, crash safety, `fsync` behavior, or atomic filesystem
  publication;
- backup integrity, provider durability, machine/power-loss behavior, or real
  provider failures;
- Access Policy, ACL, `AuthorizedOriginalReadGrant` issuance, policy/lifecycle
  revalidation, or authorized-read correctness.

The fake only enforces observable storage lifecycle, integrity, resource-bound,
opaque-reference, Workspace/Environment binding, and controlled-deletion
behavior. It never decides application authorization or claims that possession
of an object reference permits content disclosure.
