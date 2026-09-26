# HeishaMon reference provenance

The files in this directory are factual device evidence. Three of them are also packaged runtime
inputs: the backend image parses them at startup into the effective capability catalog
(`docs/ARCHITECTURE.md` §25.1). An edit changes runtime behavior. Runtime never fetches upstream.

| File | Source | Pinned at | Git blob |
|---|---|---|---|
| `MQTT-Topics.md` | `heishamon/HeishaMon` `MQTT-Topics.md`, verbatim | commit `0de4f3c02598f542e7859a772e627e5c1ebc2ce3` (release v4.2.2) | `7d22ca5dc7a752ed4a8ee0c03e743564e2bb5f7b` |
| `OptionalPCB.md` | `heishamon/HeishaMon` `OptionalPCB.md`, verbatim | commit `0de4f3c02598f542e7859a772e627e5c1ebc2ce3` (release v4.2.2) | `bae3bbda8e11fb441e34ce348d45fce0a7d65be9` |
| `realne_dane.md` | Owner device snapshot (observed TOP/XTOP names and values) | — | — |

The historical `Egyras/HeishaMon` URL redirects to `heishamon/HeishaMon`.

The git blob ids equal the upstream blob ids at the pinned commit, which proves both copies are
verbatim. A refresh copies the upstream file unchanged and updates this table in the same commit.
Upstream is never hand-edited here. When upstream is wrong, Pompa Next records the correction in
`docs/ARCHITECTURE.md` (§25.5) and in code.

`PROVENANCE.md` itself is not packaged or parsed.
