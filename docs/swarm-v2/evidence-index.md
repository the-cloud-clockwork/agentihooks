# Swarm v2 requirement-to-evidence registry

Package SV2-FND-04, registry revision 1. Generated from `docs/swarm-v2/evidence-index.json` by `python -m scripts.swarm_v2.validate_plan render`; edit the registry, never this file.

## Gates

| Gate | Packages |
|---|---|
| G0 | SV2-FND-01, SV2-FND-02, SV2-FND-03, SV2-FND-04, SV2-FND-05 |
| G1 | SV2-IDN-01, SV2-IDN-02, SV2-IDN-03, SV2-IDN-04, SV2-IDN-05, SV2-RUN-01, SV2-RUN-02, SV2-RUN-03, SV2-RUN-04, SV2-RUN-05 |
| G2 | SV2-IMG-01, SV2-IMG-02, SV2-IMG-03, SV2-IMG-04, SV2-IMG-05 |
| G3 | SV2-CTL-01, SV2-CTL-02, SV2-CTL-03, SV2-CTL-04, SV2-CTL-05, SV2-LDG-01, SV2-LDG-02, SV2-LDG-03, SV2-LDG-04, SV2-LDG-05, SV2-KUB-01, SV2-KUB-02, SV2-KUB-03, SV2-KUB-04, SV2-KUB-05, SV2-HDR-01, SV2-HDR-02, SV2-HDR-03, SV2-HDR-04, SV2-HDR-05, SV2-SEC-01, SV2-SEC-02, SV2-SEC-03, SV2-SEC-04, SV2-SEC-05, SV2-FSY-01, SV2-FSY-02, SV2-FSY-03, SV2-FSY-04, SV2-FSY-05, SV2-ACC-01, SV2-ACC-02, SV2-ACC-03, SV2-ACC-04, SV2-ACC-05 |
| G4 | SV2-SES-01, SV2-SES-02, SV2-SES-03, SV2-SES-04, SV2-SES-05, SV2-IDX-01, SV2-IDX-02, SV2-IDX-03, SV2-IDX-04, SV2-IDX-05 |
| G5 | SV2-BRP-01, SV2-BRP-02, SV2-BRP-03, SV2-BRP-04, SV2-BRP-05 |
| G6 | SV2-GRF-01, SV2-GRF-02, SV2-GRF-03, SV2-GRF-04, SV2-GRF-05, SV2-CTX-01, SV2-CTX-02, SV2-CTX-03, SV2-CTX-04, SV2-CTX-05, SV2-GAT-01, SV2-GAT-02, SV2-GAT-03, SV2-GAT-04, SV2-GAT-05, SV2-ING-01, SV2-ING-02, SV2-ING-03, SV2-ING-04, SV2-ING-05 |
| G7 | SV2-MUL-01, SV2-MUL-02, SV2-MUL-03, SV2-MUL-04, SV2-MUL-05 |
| G8 | SV2-CKP-01, SV2-CKP-02, SV2-CKP-03, SV2-CKP-04, SV2-CKP-05 |
| G10 | SV2-CAP-01, SV2-CAP-02, SV2-CAP-03, SV2-CAP-04, SV2-CAP-05, SV2-OBS-01, SV2-OBS-02, SV2-OBS-03, SV2-OBS-04, SV2-OBS-05, SV2-UX-01, SV2-UX-02, SV2-UX-03, SV2-UX-04, SV2-UX-05, SV2-PRF-01, SV2-PRF-02, SV2-PRF-03, SV2-PRF-04, SV2-PRF-05 |
| G11 | SV2-MIG-01, SV2-MIG-02, SV2-MIG-03, SV2-MIG-04, SV2-MIG-05 |
| G12 | SV2-VAL-01, SV2-VAL-02, SV2-VAL-03, SV2-VAL-04, SV2-VAL-05, SV2-GIT-01, SV2-GIT-02, SV2-GIT-03, SV2-GIT-04, SV2-GIT-05, SV2-RET-01, SV2-RET-02, SV2-RET-03, SV2-RET-04, SV2-RET-05, SV2-OPS-01, SV2-OPS-02, SV2-OPS-03, SV2-OPS-04, SV2-OPS-05, SV2-REL-01, SV2-REL-02, SV2-REL-03, SV2-REL-04, SV2-REL-05 |

## Invariants

| Invariant | Class | Packages |
|---|---|---|
| INV-R01 | execution safety | SV2-RUN-05, SV2-HDR-05 |
| INV-R02 | execution safety | SV2-RUN-04, SV2-HDR-05 |
| INV-R03 | execution safety | SV2-IDN-02, SV2-RUN-02 |
| INV-R04 | execution safety | SV2-RUN-01, SV2-HDR-04 |
| INV-R05 | execution safety | SV2-CTL-02, SV2-LDG-03 |
| INV-R06 | execution safety | SV2-RUN-03, SV2-KUB-02 |
| INV-R07 | execution safety | SV2-CTL-02, SV2-CTL-05 |
| INV-R08 | execution safety | SV2-CTL-01, SV2-LDG-02 |
| INV-R09 | scope isolation | SV2-SEC-01, SV2-SEC-02 |
| INV-R10 | execution safety | SV2-KUB-04, SV2-KUB-05 |
| INV-R11 | execution safety | SV2-RUN-01, SV2-VAL-05 |
| INV-R12 | execution safety | SV2-CAP-05 |
| INV-M01 | transcript durability | SV2-SES-04, SV2-IDX-01, SV2-VAL-03 |
| INV-M02 | transcript durability | SV2-SES-01, SV2-SES-04 |
| INV-M03 | transcript durability | SV2-SES-02 |
| INV-M04 | transcript durability | SV2-SES-03, SV2-SES-04 |
| INV-M05 | transcript durability | SV2-IDX-04, SV2-IDX-05 |
| INV-M06 | transcript durability | SV2-IDX-02, SV2-IDX-03 |
| INV-M07 | scope isolation | SV2-RET-02, SV2-CTX-04 |
| INV-M08 | knowledge integrity | SV2-GRF-05, SV2-ING-05 |
| INV-M09 | scope isolation | SV2-SEC-05, SV2-MUL-04, SV2-VAL-03 |
| INV-M10 | knowledge integrity | SV2-ING-03 |
| INV-B01 | scope isolation | SV2-GRF-03, SV2-CTX-01 |
| INV-B02 | scope isolation | SV2-IDN-01, SV2-BRP-01 |
| INV-B03 | knowledge integrity | SV2-BRP-02, SV2-GRF-05 |
| INV-B04 | knowledge integrity | SV2-CTX-02, SV2-GAT-03 |
| INV-B05 | knowledge integrity | SV2-CTX-05, SV2-GAT-04 |
| INV-B06 | scope isolation | SV2-GAT-04 |
| INV-B07 | knowledge integrity | SV2-GRF-01, SV2-GRF-04 |
| INV-B08 | knowledge integrity | SV2-GRF-04 |
| INV-B09 | scope isolation | SV2-MUL-03 |
| INV-B10 | scope isolation | SV2-GAT-02, SV2-MUL-04 |
| INV-B11 | knowledge integrity | SV2-CTX-03 |
| INV-B12 | knowledge integrity | SV2-GAT-05, SV2-ING-05 |

## Claimed packages

| Package | Repository | Gate | Claim | Evidence | Findings |
|---|---|---|---|---|---|
| SV2-FND-01 | agentihooks | G0 | complete | 4 | none |
| SV2-FND-02 | agentihooks | G0 | complete | 4 | none |
| SV2-FND-03 | agentihooks | G0 | complete | 4 | none |

## Findings

None.

## Failed experiments

None.

packages_missing_evidence: 0
