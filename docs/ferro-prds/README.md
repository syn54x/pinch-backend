# Ferro PRDs

Per ADR 0003, Pinch never works around ferro-orm gaps in domain data access —
it blocks and files a PRD (product requirements document) on ferro's issue
board instead. This directory holds the drafts, each motivated by concrete
Pinch queries. File them upstream, then link the issue back here. Once filed, the
upstream issue body is the canonical PRD; the drafts here are historical.

| PRD | Capability | Status |
|---|---|---|
| 0001 | Table joins | filed: [ferro-orm#259](https://github.com/syn54x/ferro-orm/issues/259) |
| 0002 | Aggregations & group-by | filed as workloads on existing [Epic F-6 (ferro-orm#225)](https://github.com/syn54x/ferro-orm/issues/225#issuecomment-4928762474) |
| 0003 | JSONB columns | filed: [ferro-orm#260](https://github.com/syn54x/ferro-orm/issues/260) |
| 0004 | Static typing for shadow FK columns & relation traversal | filed: [ferro-orm#290](https://github.com/syn54x/ferro-orm/issues/290) |
